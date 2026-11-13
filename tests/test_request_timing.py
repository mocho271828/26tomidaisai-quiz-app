import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from anyio.to_thread import current_default_thread_limiter, run_sync
from sqlalchemy import create_engine
from starlette.types import Message, Receive, Scope, Send

from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.middleware import PrometheusMiddleware
from quiz_app.observability.request_timing import (
    AnswerDifferenceBand,
    RequestTiming,
    TimingStage,
    current_request_timing,
    mark_handler_dispatch,
    observe_handler,
    observe_stage,
    record_answer_difference,
    record_database_work,
)
from quiz_app.observability.runtime import ObservedQueuePool


@pytest.fixture
def request_timing() -> Iterator[RequestTiming]:
    timing = RequestTiming()
    token = current_request_timing.set(timing)
    try:
        yield timing
    finally:
        current_request_timing.reset(token)


@pytest.mark.parametrize(
    "difference_ms,band",
    [
        (-2501, "below_minus_2500"),
        (-2500, "minus_2500_to_minus_251"),
        (-2499, "minus_2500_to_minus_251"),
        (-251, "minus_2500_to_minus_251"),
        (-250, "minus_250_to_minus_1"),
        (0, "0_to_250"),
        (250, "0_to_250"),
        (251, "251_to_1000"),
        (1000, "251_to_1000"),
        (1001, "1001_to_2500"),
        (2499, "1001_to_2500"),
        (2500, "1001_to_2500"),
        (2501, "2501_to_4000"),
        (4000, "2501_to_4000"),
        (4001, "4001_to_10000"),
        (10_000, "4001_to_10000"),
        (10_001, "10001_to_14000"),
        (14_000, "10001_to_14000"),
        (14_001, "above_14000"),
    ],
)
def test_answer_difference_aggregates_fixed_bands_without_raw_time(
    request_timing: RequestTiming,
    difference_ms: int,
    band: AnswerDifferenceBand,
) -> None:
    record_answer_difference(difference_ms)
    assert request_timing.answer_difference_band == band
    assert request_timing.server_timing() == b""
    metrics = QuizMetrics()
    metrics.record_answer_difference(band)
    assert (
        metrics.registry.get_sample_value("quiz_answer_receipt_difference_total", {"band": band})
        == 1
    )


@pytest.mark.asyncio
async def test_worker_context_preserves_stage_and_pool_measurements(
    request_timing: RequestTiming,
) -> None:
    engine = create_engine("sqlite://", poolclass=ObservedQueuePool)

    def project() -> str:
        with observe_handler("state_projection"):
            with engine.connect(), observe_stage("db_lock_query"):
                return "saved"

    try:
        await mark_handler_dispatch()
        assert await run_sync(project) == "saved"
    finally:
        engine.dispose()
    request_timing.finish_response()

    stages = request_timing.durations
    assert {
        "api_total",
        "pre_handler",
        "thread_wait",
        "handler",
        "response_build",
        "db_acquire",
        "db_lock_query",
        "state_projection",
    } == stages.keys()
    assert stages["handler"] == stages["state_projection"]
    assert stages["api_total"] == pytest.approx(
        stages["pre_handler"] + stages["thread_wait"] + stages["handler"] + stages["response_build"]
    )


@pytest.mark.asyncio
async def test_saturated_default_limiter_is_measured_as_thread_wait(
    request_timing: RequestTiming,
) -> None:
    limiter = current_default_thread_limiter()
    original_tokens = limiter.total_tokens
    limiter.total_tokens = 1
    await limiter.acquire()

    def accept() -> str:
        with observe_handler():
            return "accepted"

    async def dispatch() -> str:
        await mark_handler_dispatch()
        return await run_sync(accept)

    task = asyncio.create_task(dispatch())
    try:
        await asyncio.sleep(0.03)
        assert not task.done()
        assert "handler" not in request_timing.durations
    finally:
        limiter.release()
        limiter.total_tokens = original_tokens
    assert await task == "accepted"
    assert request_timing.durations["thread_wait"] >= 0.02
    assert request_timing.durations["handler"] < request_timing.durations["thread_wait"]


@pytest.mark.asyncio
async def test_handler_failure_keeps_timing_and_does_not_swallow_error(
    request_timing: RequestTiming,
) -> None:
    def fail() -> None:
        with observe_handler(), observe_stage("answer_transaction"):
            raise ValueError("controlled_failure")

    with pytest.raises(ValueError, match="controlled_failure"):
        await mark_handler_dispatch()
        await run_sync(fail)
    request_timing.finish_response()
    assert "handler" in request_timing.durations
    assert "answer_transaction" in request_timing.durations
    assert "response_build" in request_timing.durations


@pytest.mark.parametrize("stage", ["thread_wait", "recovery_wait", "common_state_wait"])
def test_stage_headers_and_metrics_have_only_duration_and_bounded_labels(
    stage: TimingStage,
) -> None:
    timing = RequestTiming()
    timing.add(stage, 0.003)
    timing.add(stage, 0.002)
    assert timing.server_timing() == f"{stage};dur=5.000".encode()
    metrics = QuizMetrics()
    metrics.record_participant_request_stage(
        method="POST",
        route="/api/v1/participant/rounds/{round_id}/answers",
        stage=stage,
        duration_seconds=0.005,
    )
    samples = metrics.render().decode()
    assert f'stage="{stage}"' in samples
    assert 'route="/api/v1/participant/rounds/{round_id}/answers"' in samples
    assert current_request_timing.get() is None


def test_finished_timing_is_idempotent_and_excludes_late_worker_stages() -> None:
    timing = RequestTiming()
    timing.count_database_work("sessions")
    timing.count_database_work("statements")
    timing.finish_response()
    durations = timing.durations.copy()
    database_work = timing.database_work.copy()
    timing.finish_response()
    timing.add("answer_transaction", 20.0)
    timing.count_database_work("statements")
    timing.count_database_work("acquisitions")
    timing.count_database_work("auth_cache_hits")
    timing.count_database_work("sessions")
    assert timing.durations == durations
    assert timing.database_work == database_work


@pytest.mark.asyncio
async def test_async_handler_dispatch_is_separate_from_sync_worker_wait(
    request_timing: RequestTiming,
) -> None:
    await mark_handler_dispatch()
    await asyncio.sleep(0.02)
    with observe_handler(is_async=True), observe_stage("participant_metadata"):
        await asyncio.sleep(0.01)
        record_database_work("sessions")
    request_timing.finish_response()
    assert "thread_wait" not in request_timing.durations
    assert request_timing.durations["dispatch_wait"] >= 0.015
    assert request_timing.durations["participant_metadata"] >= 0.005
    assert request_timing.durations["api_total"] == pytest.approx(
        request_timing.durations["pre_handler"]
        + request_timing.durations["dispatch_wait"]
        + request_timing.durations["handler"]
        + request_timing.durations["response_build"]
    )


@pytest.mark.asyncio
async def test_late_worker_database_work_cannot_mutate_finished_request(
    request_timing: RequestTiming,
) -> None:
    record_database_work("statements")
    request_timing.finish_response()
    counts = request_timing.database_work.copy()

    def continued_worker() -> None:
        record_database_work("statements")
        record_database_work("acquisitions")

    await run_sync(continued_worker)
    assert request_timing.database_work == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError(), RuntimeError("before_headers")])
async def test_before_headers_failure_records_total_and_clears_context(
    failure: BaseException,
) -> None:
    metrics = QuizMetrics()

    async def failed_app(_: Scope, __: Receive, ___: Send) -> None:
        raise failure

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_: Message) -> None:
        pytest.fail("response_headers_were_not_expected")

    scope = cast(
        Scope,
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/participant/session",
            "route": SimpleNamespace(path="/api/v1/participant/session"),
        },
    )
    with pytest.raises(type(failure)):
        await PrometheusMiddleware(failed_app, metrics)(scope, receive, send)
    assert current_request_timing.get() is None
    samples = metrics.render().decode()
    assert (
        'quiz_participant_request_stage_duration_seconds_count{method="GET",'
        'route="/api/v1/participant/session",stage="api_total"} 1.0'
    ) in samples
    assert 'stage="handler"' not in samples
