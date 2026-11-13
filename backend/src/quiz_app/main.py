import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated

import logfire
from fastapi import Depends, FastAPI, Response, status
from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from quiz_app.async_db import get_async_engine
from quiz_app.audience.async_service import (
    close_audience_async_service,
    reset_audience_async_service,
)
from quiz_app.audience.error_handlers import install_audience_error_handlers
from quiz_app.audience.projection import get_audience_projection_service
from quiz_app.audience.router import router as audience_router
from quiz_app.config import get_settings
from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.db import get_engine
from quiz_app.health import check_readiness
from quiz_app.observability import QuizMetrics
from quiz_app.observability.answer_diagnostics import preserve_diagnostic_codes
from quiz_app.observability.middleware import PrometheusMiddleware
from quiz_app.observability.realtime_failures import preserve_realtime_failure_fields
from quiz_app.observability.runtime import monitor_runtime
from quiz_app.operator.control_router import router as operator_control_router
from quiz_app.operator.error_handlers import install_operator_error_handlers
from quiz_app.operator.router import router as operator_router
from quiz_app.operator.runtime_isolation import enforce_production_runtime_isolation
from quiz_app.quiz_configuration import get_quiz_settings
from quiz_app.realtime.answer_notifications import AnswerNotificationCoalescer
from quiz_app.realtime.contracts import install_realtime_openapi
from quiz_app.realtime.notification_dispatcher import (
    NotificationDispatcher,
    QueuedAsyncRealtimePublisher,
)
from quiz_app.realtime.redis_adapter import (
    AsyncRealtimePublisher,
    RedisRealtimeSubscriptionFactory,
    get_async_redis_client,
    get_sync_redis_client,
)
from quiz_app.realtime.router import router as realtime_router
from quiz_app.realtime.state_stream import StateStreamFactory
from quiz_app.result_worker_process import (
    AutomaticResultProcessRunner,
    AutomaticResultRunner,
    ResultWorkerCleanupError,
)

AUTOMATIC_RESULT_WORKER_RESTART_DELAYS_SECONDS = (0.1, 0.5, 1.0, 2.0)


def _preserve_runtime_log_fields(match: logfire.ScrubMatch) -> str | None:
    """固定診断codeと安全なWS sourceだけを残し、未知値の秘匿を維持する。"""

    return preserve_diagnostic_codes(match) or preserve_realtime_failure_fields(match)


async def _supervise_automatic_result_worker(
    application: FastAPI,
    publisher: AutomaticResultRunner,
    stop_requested: asyncio.Event,
) -> None:
    """workerの予期終了を検知し、上限付きbackoff後に再起動する。"""

    retry_index = 0
    worker_task: asyncio.Task[None] | None = None
    stop_waiter = asyncio.create_task(
        stop_requested.wait(),
        name="automatic-result-publisher-stop",
    )
    try:
        while not stop_requested.is_set():
            application.state.automatic_result_worker_alive = True
            application.state.automatic_result_worker_error = None
            worker_task = asyncio.create_task(
                publisher.run(stop_requested),
                name="automatic-result-publisher",
            )
            completed, _ = await asyncio.wait(
                {worker_task, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_waiter in completed:
                if not worker_task.done():
                    await asyncio.wait({worker_task})
                if not worker_task.cancelled():
                    worker_task.exception()
                break

            application.state.automatic_result_worker_alive = False
            if worker_task.cancelled():
                error = None
                error_type = "CancelledError"
            else:
                error = worker_task.exception()
                error_type = type(error).__name__ if error is not None else "UnexpectedWorkerExit"
            application.state.automatic_result_worker_error = error_type
            if isinstance(error, ResultWorkerCleanupError):
                logfire.error("result worker cleanup failed; restart disabled", _exc_info=error)
                await stop_waiter
                break
            restart_delay = AUTOMATIC_RESULT_WORKER_RESTART_DELAYS_SECONDS[
                min(
                    retry_index,
                    len(AUTOMATIC_RESULT_WORKER_RESTART_DELAYS_SECONDS) - 1,
                )
            ]
            retry_index += 1
            logfire.error(
                "automatic result publisher stopped unexpectedly",
                error_type=error_type,
                restart_delay_seconds=restart_delay,
                _exc_info=error if error is not None else False,
            )
            try:
                await asyncio.wait_for(stop_requested.wait(), timeout=restart_delay)
            except TimeoutError:
                continue
    finally:
        application.state.automatic_result_worker_alive = False
        stop_waiter.cancel()
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
        await asyncio.gather(
            *(task for task in (worker_task, stop_waiter) if task is not None),
            return_exceptions=True,
        )


def _close_sync_redis(client: SyncRedis) -> None:
    try:
        client.close()
    finally:
        get_sync_redis_client.cache_clear()


async def _close_async_redis(client: AsyncRedis) -> None:
    try:
        await client.aclose()
    finally:
        get_async_redis_client.cache_clear()


async def _dispose_async_database(engine: AsyncEngine) -> None:
    await close_audience_async_service()
    try:
        await engine.dispose()
    finally:
        reset_audience_async_service()
        get_async_engine.cache_clear()


async def _dispose_sync_database(engine: Engine) -> None:
    await asyncio.to_thread(engine.dispose)


async def _stop_application_tasks(
    stop_requested: asyncio.Event,
    supervisor: asyncio.Task[None],
    runtime: asyncio.Task[None],
) -> None:
    stop_requested.set()
    results = await asyncio.gather(supervisor, runtime, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """一つの終了処理が失敗しても、取得済み資源すべての回収を試みる。"""

    settings = get_settings()
    logfire.configure(
        send_to_logfire=settings.logfire_send_to_logfire,
        scrubbing=logfire.ScrubbingOptions(callback=_preserve_runtime_log_fields),
    )
    async with AsyncExitStack() as resources:
        engine = get_engine()
        resources.push_async_callback(_dispose_sync_database, engine)
        await asyncio.to_thread(enforce_production_runtime_isolation, settings, engine)
        application.state.quiz_metrics.register_database_pool(engine.pool)
        sync_redis = await asyncio.to_thread(get_sync_redis_client)
        resources.callback(_close_sync_redis, sync_redis)
        async_engine = get_async_engine()
        resources.push_async_callback(_dispose_async_database, async_engine)
        application.state.quiz_metrics.register_database_pool(
            async_engine.sync_engine.pool, role="async"
        )
        async_redis = await asyncio.to_thread(get_async_redis_client)
        resources.push_async_callback(_close_async_redis, async_redis)
        resources.push_async_callback(application.state.realtime_subscription_factory.close)
        await application.state.realtime_subscription_factory.start()
        resources.push_async_callback(application.state.realtime_state_factory.close)
        await application.state.realtime_state_factory.start()
        get_audience_projection_service().personal_results.start(application.state.quiz_metrics)
        notification_dispatcher = NotificationDispatcher(
            AsyncRealtimePublisher(async_redis, application.state.quiz_metrics),
            application.state.realtime_state_factory,
            timeout=settings.redis_operation_timeout_seconds,
        )
        application.state.notification_dispatcher = notification_dispatcher
        resources.push_async_callback(notification_dispatcher.close)
        await notification_dispatcher.start()
        answer_notifications = AnswerNotificationCoalescer(
            QueuedAsyncRealtimePublisher(notification_dispatcher),
            operation_timeout_seconds=settings.redis_operation_timeout_seconds,
        )
        application.state.answer_notifications = answer_notifications
        resources.push_async_callback(answer_notifications.close)
        await answer_notifications.start()
        stop_requested = asyncio.Event()
        runtime_task = asyncio.create_task(
            monitor_runtime(application.state.quiz_metrics, stop_requested),
            name="runtime-metrics",
        )
        application.state.automatic_result_worker_alive = True
        application.state.automatic_result_worker_error = None
        supervisor_task = asyncio.create_task(
            _supervise_automatic_result_worker(
                application, AutomaticResultProcessRunner(), stop_requested
            ),
            name="automatic-result-publisher-supervisor",
        )
        resources.push_async_callback(
            _stop_application_tasks, stop_requested, supervisor_task, runtime_task
        )
        yield


def create_app(metrics: QuizMetrics | None = None) -> FastAPI:
    """HTTP applicationを生成する。"""

    settings = get_settings()
    get_quiz_settings()
    application_metrics = metrics if metrics is not None else QuizMetrics()
    application = FastAPI(
        title="富大祭クイズ API",
        version=settings.app_version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.quiz_metrics = application_metrics
    application.state.realtime_subscription_factory = RedisRealtimeSubscriptionFactory(
        metrics=application_metrics,
    )
    application.state.realtime_state_factory = StateStreamFactory(
        application.state.realtime_subscription_factory,
        metrics=application_metrics,
    )
    application.add_middleware(PrometheusMiddleware, metrics=application_metrics)
    install_operator_error_handlers(application)
    install_audience_error_handlers(application)
    application.include_router(operator_router)
    application.include_router(operator_control_router)
    application.include_router(audience_router)
    application.include_router(realtime_router)
    install_realtime_openapi(application)

    @application.get(
        "/api/health/live",
        response_model=LiveHealthResponse,
        tags=["health"],
    )
    async def liveness() -> LiveHealthResponse:
        return LiveHealthResponse(version=settings.app_version)

    @application.get(
        "/api/health/ready",
        response_model=ReadinessHealthResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessHealthResponse}},
        tags=["health"],
    )
    def readiness(
        response: Response,
        readiness_status: Annotated[ReadinessHealthResponse, Depends(check_readiness)],
    ) -> ReadinessHealthResponse:
        if readiness_status.status == "not_ready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness_status

    @application.get("/internal/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(
            content=application_metrics.render(),
            headers={
                "Cache-Control": "no-store",
                "Content-Type": application_metrics.content_type,
            },
        )

    return application


app = create_app()
