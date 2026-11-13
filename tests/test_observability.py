import asyncio
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import logfire
import pytest
from fastapi import Request, WebSocket
from httpx import ASGITransport, AsyncClient
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, ProcessCollector
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.types import Message, Receive, Scope, Send

from quiz_app.main import create_app
from quiz_app.observability import QuizMetrics
from quiz_app.observability.metrics import DatabasePoolReader
from quiz_app.observability.middleware import PrometheusMiddleware
from quiz_app.realtime.redis_adapter import (
    RealtimePublisher,
    RedisHealthProbe,
    get_realtime_publisher,
)
from quiz_app.realtime.router import _send_polling_fallback

RAW_IDENTIFIER = "9f51f87f-97dc-4c52-a4c7-27af2138e78e"
RAW_IDENTIFIER_AS_UUID = UUID(RAW_IDENTIFIER)
RAW_PATH_MARKER = "opaque-value-must-not-be-a-label"


def test_each_app_metrics_registry_is_isolated_and_process_collector_is_explicit() -> None:
    first = QuizMetrics()
    second = QuizMetrics()

    first.record_redis_publish(audience="participant", succeeded=True)

    assert first.registry is not second.registry
    assert first.registry is not REGISTRY
    assert isinstance(first.process_collector, ProcessCollector)
    assert 'audience="participant"' in first.render().decode()
    assert 'audience="participant"' not in second.render().decode()


def test_create_app_defaults_and_request_dependencies_share_only_their_app_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_application = create_app()
    second_application = create_app()
    first_metrics = cast(QuizMetrics, first_application.state.quiz_metrics)
    second_metrics = cast(QuizMetrics, second_application.state.quiz_metrics)
    monkeypatch.setattr(
        "quiz_app.realtime.redis_adapter.get_sync_redis_client",
        SuccessfulRedisClient,
    )
    request = Request(cast(Scope, {"type": "http", "app": first_application}))

    publisher = get_realtime_publisher(request)
    assert publisher.notify(RAW_IDENTIFIER_AS_UUID, ("participant",))

    assert first_metrics is not second_metrics
    assert 'audience="participant"' in first_metrics.render().decode()
    assert 'audience="participant"' not in second_metrics.render().decode()


@pytest.mark.asyncio
async def test_metrics_endpoint_uses_templates_and_never_observes_itself_or_raw_paths() -> None:
    metrics = QuizMetrics()
    application = create_app(metrics)

    @application.get("/diagnostics/items/{item_id}")
    async def diagnostic_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        first_scrape = await client.get("/internal/metrics")
        response = await client.get(f"/diagnostics/items/{RAW_IDENTIFIER}")
        missing = await client.get(f"/{RAW_PATH_MARKER}")
        second_scrape = await client.get("/internal/metrics")
        openapi = await client.get("/api/openapi.json")

    assert first_scrape.status_code == 200
    assert first_scrape.headers["content-type"] == CONTENT_TYPE_LATEST
    assert first_scrape.headers["cache-control"] == "no-store"
    assert response.status_code == 200
    assert missing.status_code == 404
    samples = second_scrape.text
    assert 'route="/diagnostics/items/{item_id}"' in samples
    assert 'route="unmatched"' in samples
    assert 'route="/internal/metrics"' not in samples
    assert RAW_IDENTIFIER not in samples
    assert RAW_PATH_MARKER not in samples
    assert "/internal/metrics" not in openapi.json()["paths"]


class CompletePool:
    def checkedin(self) -> int:
        return 3

    def checkedout(self) -> int:
        return 2

    def overflow(self) -> int:
        return -4

    def size(self) -> int:
        return 5


class PoolWithoutAggregateReaders:
    pass


def test_database_pool_collector_is_idempotent_bounded_and_clamps_negative_overflow() -> None:
    metrics = QuizMetrics()
    pool = CompletePool()

    metrics.register_database_pool(pool)
    metrics.register_database_pool(pool)
    samples = metrics.render().decode()

    assert 'quiz_db_pool_connections{state="checked_in"} 3.0' in samples
    assert 'quiz_db_pool_connections{state="checked_out"} 2.0' in samples
    assert 'quiz_db_pool_connections{state="overflow"} 0.0' in samples
    assert 'quiz_db_pool_connections{state="size"} 5.0' in samples
    assert "quiz_db_pool_metrics_supported 1.0" in samples


def test_database_pool_collector_reports_unsupported_instead_of_breaking_scrape() -> None:
    metrics = QuizMetrics()
    unsupported = cast(DatabasePoolReader, PoolWithoutAggregateReaders())

    metrics.register_database_pool(unsupported)

    assert "quiz_db_pool_metrics_supported 0.0" in metrics.render().decode()


class SuccessfulRedisClient:
    def publish(self, _: str, __: str) -> int:
        return 1

    def ping(self) -> bool:
        return True


class UnavailableRedisClient:
    def publish(self, _: str, __: str) -> int:
        raise RedisConnectionError("dependency unavailable")

    def ping(self) -> bool:
        raise RedisConnectionError("dependency unavailable")


def test_redis_publish_results_and_dependency_health_have_only_bounded_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = QuizMetrics()
    monkeypatch.setattr(logfire, "exception", lambda *_, **__: None)

    assert RealtimePublisher(SuccessfulRedisClient(), metrics).notify(
        RAW_IDENTIFIER_AS_UUID,
        ("operator",),
    )
    assert not RealtimePublisher(UnavailableRedisClient(), metrics).notify(
        RAW_IDENTIFIER_AS_UUID,
        ("participant",),
    )
    assert RedisHealthProbe(SuccessfulRedisClient(), metrics).status() == "ok"
    assert RedisHealthProbe(UnavailableRedisClient(), metrics).status() == "degraded"
    samples = metrics.render().decode()

    assert 'quiz_redis_publish_total{audience="operator",result="success"} 1.0' in samples
    assert 'quiz_redis_publish_total{audience="participant",result="error"} 1.0' in samples
    assert "quiz_redis_dependency_up 0.0" in samples
    assert RAW_IDENTIFIER not in samples


async def _unused_receive() -> Message:
    return {"type": "http.disconnect"}


async def _record_send(_: Message) -> None:
    return None


def _http_scope(route: str) -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "method": "GET",
            "path": "/diagnostics/value",
            "route": SimpleNamespace(path=route),
        },
    )


@pytest.mark.asyncio
async def test_unhandled_http_exception_is_5xx_and_always_clears_in_progress() -> None:
    metrics = QuizMetrics()

    async def failing_app(_: Scope, __: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("controlled-test-failure")

    middleware = PrometheusMiddleware(failing_app, metrics)
    with pytest.raises(RuntimeError, match="controlled-test-failure"):
        await middleware(
            _http_scope("/diagnostics/{value}"),
            _unused_receive,
            _record_send,
        )

    samples = metrics.render().decode()
    assert 'status_class="5xx"' in samples
    assert 'quiz_http_requests_in_progress{method="GET",route="unmatched"} 0.0' in samples


@pytest.mark.asyncio
async def test_cancelled_http_request_is_not_misclassified_as_5xx() -> None:
    metrics = QuizMetrics()

    async def cancelled_app(_: Scope, __: Receive, ___: Send) -> None:
        raise asyncio.CancelledError

    middleware = PrometheusMiddleware(cancelled_app, metrics)
    with pytest.raises(asyncio.CancelledError):
        await middleware(
            _http_scope("/diagnostics/{value}"),
            _unused_receive,
            _record_send,
        )

    samples = metrics.render().decode()
    assert 'status_class="other"' in samples
    assert 'status_class="5xx"' not in samples
    assert 'quiz_http_requests_in_progress{method="GET",route="unmatched"} 0.0' in samples


@pytest.mark.parametrize(
    ("send_failure", "expected_status_class"),
    [
        (asyncio.CancelledError(), "other"),
        (OSError("controlled-send-failure"), "5xx"),
    ],
)
@pytest.mark.asyncio
async def test_http_response_start_is_counted_only_after_successful_send(
    send_failure: BaseException,
    expected_status_class: str,
) -> None:
    metrics = QuizMetrics()

    async def response_app(_: Scope, __: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})

    async def failing_send(_: Message) -> None:
        raise send_failure

    middleware = PrometheusMiddleware(response_app, metrics)
    with pytest.raises(type(send_failure)):
        await middleware(
            _http_scope("/diagnostics/{value}"),
            _unused_receive,
            failing_send,
        )

    samples = metrics.render().decode()
    assert f'status_class="{expected_status_class}"' in samples
    assert 'status_class="2xx"' not in samples
    assert 'quiz_http_requests_in_progress{method="GET",route="unmatched"} 0.0' in samples


def _websocket_scope(path: str) -> Scope:
    return cast(Scope, {"type": "websocket", "path": path})


@pytest.mark.parametrize(
    ("path", "audience", "close_code", "outcome", "code_class"),
    [
        ("/api/v1/participant/realtime", "participant", 1000, "normal", "1xxx"),
        (
            "/api/v1/operator/realtime",
            "operator",
            4401,
            "authentication_required",
            "4xxx",
        ),
        (
            "/api/v1/participant/realtime",
            "participant",
            4789,
            "server_close",
            "4xxx",
        ),
    ],
)
@pytest.mark.asyncio
async def test_websocket_labels_use_audience_outcome_and_code_class_only(
    path: str,
    audience: str,
    close_code: int,
    outcome: str,
    code_class: str,
) -> None:
    metrics = QuizMetrics()

    async def websocket_app(_: Scope, __: Receive, send: Send) -> None:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": close_code, "reason": ""})

    middleware = PrometheusMiddleware(websocket_app, metrics)
    await middleware(
        _websocket_scope(path),
        _unused_receive,
        _record_send,
    )
    samples = metrics.render().decode()

    assert f'quiz_websocket_attempts_total{{audience="{audience}"}} 1.0' in samples
    assert (
        f'quiz_websocket_closes_total{{audience="{audience}",code_class="{code_class}",'
        f'outcome="{outcome}"}} 1.0'
    ) in samples
    assert f'quiz_websocket_active{{audience="{audience}"}} 0.0' in samples
    assert RAW_IDENTIFIER not in samples
    websocket_label_sets = "\n".join(
        line.partition("}")[0]
        for line in samples.splitlines()
        if line.startswith("quiz_websocket_") and "{" in line
    )
    assert str(close_code) not in websocket_label_sets


@pytest.mark.asyncio
async def test_failed_websocket_accept_is_not_counted_as_active_or_accepted_close() -> None:
    metrics = QuizMetrics()

    async def websocket_app(_: Scope, __: Receive, send: Send) -> None:
        await send({"type": "websocket.accept"})

    async def failing_send(_: Message) -> None:
        raise OSError("controlled-send-failure")

    middleware = PrometheusMiddleware(websocket_app, metrics)
    with pytest.raises(OSError, match="controlled-send-failure"):
        await middleware(
            _websocket_scope("/api/v1/participant/realtime"),
            _unused_receive,
            failing_send,
        )
    samples = metrics.render().decode()

    assert 'quiz_websocket_active{audience="participant"}' not in samples
    assert 'outcome="application_exit"' in samples


class FailingFallbackWebSocket:
    def __init__(self, metrics: QuizMetrics) -> None:
        state = SimpleNamespace(quiz_metrics=metrics)
        self.scope: Scope = cast(Scope, {"app": SimpleNamespace(state=state)})

    async def send_json(self, _: object) -> None:
        raise RuntimeError("client delivery failed")

    async def close(self, *, code: int) -> None:
        del code


@pytest.mark.asyncio
async def test_polling_fallback_counts_decision_once_even_when_delivery_fails() -> None:
    metrics = QuizMetrics()
    websocket = cast(WebSocket, FailingFallbackWebSocket(metrics))

    await _send_polling_fallback(websocket, "participant")

    async def close_with_fallback_code(_: Scope, __: Receive, send: Send) -> None:
        await send({"type": "websocket.close", "code": 1013, "reason": ""})

    await PrometheusMiddleware(close_with_fallback_code, metrics)(
        _websocket_scope("/api/v1/participant/realtime"),
        _unused_receive,
        _record_send,
    )

    samples = metrics.render().decode()
    assert (
        'quiz_realtime_polling_fallback_total{audience="participant",'
        'reason="realtime_unavailable"} 1.0'
    ) in samples
