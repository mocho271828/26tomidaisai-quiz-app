"""async通知の失敗境界と、実pool・SQL eventによる低cardinality計測の回帰。"""

import asyncio
import sqlite3
import ssl
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.util import greenlet_spawn

from quiz_app.async_db import ObservedAsyncQueuePool
from quiz_app.audience.async_service import (
    AudienceAsyncService,
    CommittedParticipantAnswer,
    get_audience_async_service,
)
from quiz_app.audience.contracts import AnswerReceiptResponse, SubmitAnswerRequest
from quiz_app.audience.router import router
from quiz_app.config import PARTICIPANT_SESSION_COOKIE_NAME, Settings
from quiz_app.domain.types import AcceptedAnswer
from quiz_app.observability.database_work import observe_database_statements
from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.middleware import PrometheusMiddleware
from quiz_app.observability.request_timing import RequestTiming, current_request_timing
from quiz_app.observability.runtime import ObservedQueuePool
from quiz_app.realtime import redis_adapter
from quiz_app.realtime.answer_notifications import AnswerNotificationCoalescer
from quiz_app.realtime.contracts import StateInvalidatedMessage
from quiz_app.realtime.redis_adapter import AsyncRealtimePublisher
from quiz_app.realtime.redis_tls import SharedAsyncSSLConnection

EVENT_ID = UUID("42000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("42000000-0000-4000-8000-000000000002")
OPTION_ID = UUID("42000000-0000-4000-8000-000000000003")
ANSWER_ID = UUID("42000000-0000-4000-8000-000000000004")
CLIENT_ANSWER_ID = UUID("42000000-0000-4000-8000-000000000005")
SESSION_FIXTURE = "runtime-participant-fixture"


class CommittedAnswerService:
    """永続化境界だけを代替し、通知前にcommit済みであることを検査する。"""

    def __init__(self) -> None:
        self.is_committed = False

    async def submit_answer(
        self, token: str | None, round_id: UUID, request: SubmitAnswerRequest
    ) -> CommittedParticipantAnswer:
        assert token == SESSION_FIXTURE
        assert round_id == ROUND_ID
        assert request.option_id == OPTION_ID
        self.is_committed = True
        return CommittedParticipantAnswer(
            event_id=EVENT_ID,
            answer=AcceptedAnswer(
                timing_basis="server_received_v1",
                client_elapsed_ms=None,
                answer_id=ANSWER_ID,
                round_id=round_id,
                option_id=request.option_id,
                client_answer_id=request.client_answer_id,
                received_at=datetime(2026, 9, 30, tzinfo=UTC),
                is_replay=False,
            ),
        )


class GatedRedisClient:
    def __init__(
        self, service: CommittedAnswerService, failure: RedisError | OSError | None
    ) -> None:
        self.service = service
        self.failure = failure
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.message: StateInvalidatedMessage | None = None

    async def publish(self, channel: str, message: str) -> int:
        assert self.service.is_committed
        self.message = StateInvalidatedMessage.model_validate_json(message)
        assert channel == f"quiz:realtime:operator:{EVENT_ID}"
        self.started.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RedisConnectionError("fixture unavailable"), OSError()])
async def test_answer_receipt_finishes_during_blocked_publish_and_survives_notification_failure(
    monkeypatch: pytest.MonkeyPatch, failure: RedisError | OSError | None
) -> None:
    service = CommittedAnswerService()
    redis = GatedRedisClient(service, failure)
    metrics = QuizMetrics()
    publisher = AsyncRealtimePublisher(redis, metrics)
    application = FastAPI()
    application.include_router(router)
    application.add_middleware(PrometheusMiddleware, metrics=metrics)

    async def audience_service() -> AudienceAsyncService:
        return cast(AudienceAsyncService, service)

    notifications = AnswerNotificationCoalescer(
        publisher, operation_timeout_seconds=1, window_seconds=0.001
    )
    application.state.answer_notifications = notifications
    await notifications.start()

    errors: list[tuple[str, str]] = []

    def record_error(error: BaseException, boundary: str, *, audience: str) -> None:
        assert error is failure
        errors.append((boundary, audience))

    monkeypatch.setattr(redis_adapter, "log_realtime_failure", record_error)
    application.dependency_overrides[get_audience_async_service] = audience_service
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="https://fixture.invalid",
        cookies={PARTICIPANT_SESSION_COOKIE_NAME: SESSION_FIXTURE},
    ) as client:
        response_task = asyncio.create_task(
            client.post(
                f"/api/v1/participant/rounds/{ROUND_ID}/answers",
                json={
                    "round_version": 1,
                    "client_elapsed_ms": 1000,
                    "option_id": str(OPTION_ID),
                    "client_answer_id": str(CLIENT_ANSWER_ID),
                },
            )
        )
        try:
            await asyncio.wait_for(redis.started.wait(), timeout=1)
            assert service.is_committed
            response = await asyncio.wait_for(response_task, timeout=0.1)
            assert not redis.release.is_set()
        finally:
            redis.release.set()
            if not response_task.done():
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
            await notifications.close()

    assert response.status_code == 200
    receipt = AnswerReceiptResponse.model_validate(response.json())
    assert receipt.answer_id == ANSWER_ID and not receipt.is_replay
    assert redis.message is not None and redis.message.audience == "operator"
    assert response.headers["Cache-Control"] == "no-store"
    assert "redis_publish;dur=" not in response.headers["Server-Timing"]
    result = "success" if failure is None else "error"
    assert (
        metrics.registry.get_sample_value(
            "quiz_redis_publish_total", {"audience": "operator", "result": result}
        )
        == 1
    )
    assert errors == ([] if failure is None else [("redis_publish", "operator")])
    rendered = metrics.render().decode()
    for private_value in (str(EVENT_ID), str(ROUND_ID), str(ANSWER_ID), SESSION_FIXTURE):
        assert private_value not in rendered


@pytest.mark.asyncio
async def test_publish_cancellation_propagates_without_redis_failure_or_commit_reversal() -> None:
    service = CommittedAnswerService()
    service.is_committed = True
    redis = GatedRedisClient(service, None)
    metrics = QuizMetrics()
    task = asyncio.create_task(
        AsyncRealtimePublisher(redis, metrics).notify(EVENT_ID, ("operator",))
    )
    try:
        await asyncio.wait_for(redis.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.is_committed
        for result in ("success", "error"):
            assert (
                metrics.registry.get_sample_value(
                    "quiz_redis_publish_total", {"audience": "operator", "result": result}
                )
                is None
            )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_publish_failure_continues_other_audiences_and_records_fixed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[StateInvalidatedMessage] = []

    class SelectivelyFailingRedis:
        async def publish(self, _channel: str, payload: str) -> int:
            message = StateInvalidatedMessage.model_validate_json(payload)
            received.append(message)
            if message.audience == "operator":
                raise RedisConnectionError("fixture unavailable")
            return 1

    monkeypatch.setattr(redis_adapter, "log_realtime_failure", lambda *_, **__: None)
    metrics = QuizMetrics()
    publisher = AsyncRealtimePublisher(SelectivelyFailingRedis(), metrics)
    assert not await publisher.notify(EVENT_ID, ("operator", "participant"))
    assert [message.audience for message in received] == ["operator", "participant"]
    for audience, result in (
        ("operator", "error"),
        ("participant", "success"),
    ):
        assert (
            metrics.registry.get_sample_value(
                "quiz_redis_publish_total", {"audience": audience, "result": result}
            )
            == 1
        )


@pytest.mark.asyncio
async def test_async_redis_publish_pool_reuses_verified_tls_and_bounds_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        redis_url="rediss://publish.fixture.invalid:6379/0",
        redis_connect_timeout_seconds=0.02,
        redis_operation_timeout_seconds=0.01,
    )
    context = ssl.create_default_context()
    context_calls: list[ssl.SSLContext] = []

    def create_context() -> ssl.SSLContext:
        context_calls.append(context)
        return context

    async def connect(_connection: SharedAsyncSSLConnection) -> None:
        return None

    async def can_read(_connection: SharedAsyncSSLConnection) -> bool:
        return False

    monkeypatch.setattr(redis_adapter, "get_settings", lambda: settings)
    monkeypatch.setattr(redis_adapter.ssl, "create_default_context", create_context)
    # transportだけを代替し、redis-py本体の上限・待ち・返却処理は実行する。
    monkeypatch.setattr(SharedAsyncSSLConnection, "connect", connect)
    monkeypatch.setattr(SharedAsyncSSLConnection, "can_read_destructive", can_read)
    redis_adapter.get_async_redis_client.cache_clear()
    client = redis_adapter.get_async_redis_client()
    pool = client.connection_pool
    assert isinstance(client, Redis) and isinstance(pool, BlockingConnectionPool)
    borrowed = []
    try:
        assert client is redis_adapter.get_async_redis_client()
        assert context_calls == [context]
        assert pool.max_connections == 10 and pool.timeout == 0.01
        for _ in range(10):
            connection = await pool.get_connection()
            borrowed.append(connection)
            assert isinstance(connection, SharedAsyncSSLConnection)
            assert connection.ssl_context.get() is context
            assert connection.socket_connect_timeout == 0.02
            assert connection.socket_timeout == 0.01
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        with pytest.raises(RedisConnectionError, match="No connection available"):
            await asyncio.wait_for(pool.get_connection(), timeout=1)
        released = borrowed.pop()
        await pool.release(released)
        recovered = await asyncio.wait_for(pool.get_connection(), timeout=1)
        borrowed.append(recovered)
        assert recovered is released
    finally:
        for connection in borrowed:
            await pool.release(connection)
        await client.aclose()
        redis_adapter.get_async_redis_client.cache_clear()


@pytest.mark.asyncio
async def test_sync_and_async_pool_metrics_sum_borrowed_connections_and_recover_after_timeout() -> (
    None
):
    metrics = QuizMetrics()
    sync_pool = ObservedQueuePool(
        lambda: sqlite3.connect(":memory:"), pool_size=3, max_overflow=0, timeout=0.01
    )
    async_pool = ObservedAsyncQueuePool(
        lambda: sqlite3.connect(":memory:"), pool_size=10, max_overflow=0, timeout=0.01
    )
    metrics.register_database_pool(sync_pool, role="sync")
    metrics.register_database_pool(async_pool, role="async")
    # 同じroleを再登録しても、重複加算するcollectorを作らない。
    metrics.register_database_pool(async_pool, role="async")
    borrowed_sync = []
    borrowed_async = []
    timing = RequestTiming()
    token = current_request_timing.set(timing)
    try:
        borrowed_sync = [sync_pool.connect() for _ in range(3)]
        for _ in range(10):
            borrowed_async.append(await greenlet_spawn(async_pool.connect))
        with pytest.raises(PoolTimeoutError):
            sync_pool.connect()
        with pytest.raises(PoolTimeoutError):
            await greenlet_spawn(async_pool.connect)
        assert timing.database_work["acquisitions"] == 15
        assert (
            metrics.registry.get_sample_value("quiz_db_pool_connections", {"state": "checked_out"})
            == 13
        )
        assert (
            metrics.registry.get_sample_value("quiz_db_pool_connections", {"state": "size"}) == 13
        )
        for role, count in (("sync", 3), ("async", 10)):
            assert (
                metrics.registry.get_sample_value(
                    "quiz_db_pool_role_connections", {"role": role, "state": "checked_out"}
                )
                == count
            )
        assert metrics.registry.get_sample_value("quiz_db_pool_metrics_supported") == 1
        assert (
            metrics.registry.get_sample_value(
                "quiz_db_connection_acquisition_seconds_count", {"result": "success"}
            )
            == 13
        )
        assert (
            metrics.registry.get_sample_value(
                "quiz_db_connection_acquisition_seconds_count", {"result": "error"}
            )
            == 2
        )
    finally:
        current_request_timing.reset(token)
        for connection in borrowed_sync:
            connection.close()
        for connection in borrowed_async:
            await greenlet_spawn(connection.close)
        sync_pool.dispose()
        async_pool.dispose()
    assert (
        metrics.registry.get_sample_value("quiz_db_pool_connections", {"state": "checked_out"}) == 0
    )
    for role in ("sync", "async"):
        assert (
            metrics.registry.get_sample_value(
                "quiz_db_pool_role_connections", {"role": role, "state": "checked_out"}
            )
            == 0
        )


def test_sql_event_counts_request_work_without_sql_values_or_post_response_increments() -> None:
    engine = create_engine("sqlite://", poolclass=ObservedQueuePool, pool_size=1, max_overflow=0)
    observe_database_statements(engine)
    metrics = QuizMetrics()
    metrics.register_database_pool(engine.pool)
    private_value = "fixture-bound-value-never-a-metric-label"
    timing = RequestTiming()
    token = current_request_timing.set(timing)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT :value"), {"value": private_value}).scalar_one()
                == private_value
            )
            connection.commit()
            assert connection.execute(text("SELECT 7")).scalar_one() == 7
            connection.rollback()
            assert timing.database_work == {"acquisitions": 1, "statements": 2}
            timing.finish_response()
            assert connection.execute(text("SELECT 8")).scalar_one() == 8
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 9")).scalar_one() == 9
        assert timing.database_work == {"acquisitions": 1, "statements": 2}
    finally:
        current_request_timing.reset(token)
        engine.dispose()
    metrics.record_participant_database_work(
        method="POST",
        route="/api/v1/participant/rounds/{round_id}/answers",
        counts=timing.database_work,
    )
    for kind, count in (
        ("statements", 2),
        ("acquisitions", 1),
        ("sessions", 0),
        ("auth_cache_hits", 0),
    ):
        assert (
            metrics.registry.get_sample_value(
                "quiz_participant_database_work_sum",
                {
                    "method": "POST",
                    "route": "/api/v1/participant/rounds/{round_id}/answers",
                    "kind": kind,
                },
            )
            == count
        )
    rendered = metrics.render().decode()
    assert private_value not in rendered and "SELECT" not in rendered and "sqlite" not in rendered
