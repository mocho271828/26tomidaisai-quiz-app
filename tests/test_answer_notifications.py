import asyncio
from collections.abc import AsyncIterator, Sequence
from uuid import UUID

import pytest
import pytest_asyncio
from redis.exceptions import RedisError

from quiz_app.realtime.answer_notifications import AnswerNotificationCoalescer
from quiz_app.realtime.contracts import RealtimeAudience
from quiz_app.realtime.redis_adapter import AsyncRealtimePublisher, InvalidationCause

EVENT_ID = UUID(int=1)


class Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class Publisher(AsyncRealtimePublisher):
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, tuple[RealtimeAudience, ...], InvalidationCause]] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.failure: RedisError | None = None

    async def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause = "operator_mutation",
    ) -> bool:
        self.calls.append((event_id, tuple(audiences), cause))
        self.entered.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.failure is not None:
            raise self.failure
        return True


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def notifications() -> AsyncIterator[tuple[AnswerNotificationCoalescer, Publisher, Clock]]:
    publisher = Publisher()
    clock = Clock()
    worker = AnswerNotificationCoalescer(publisher, operation_timeout_seconds=0.1, clock=clock)
    await worker.start()
    try:
        yield worker, publisher, clock
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_burst_has_fixed_500ms_window_and_operator_only_cause(
    notifications: tuple[AnswerNotificationCoalescer, Publisher, Clock],
) -> None:
    worker, publisher, clock = notifications
    assert worker.enqueue(EVENT_ID)
    await settle()
    clock.value = 0.499
    for _ in range(600):
        assert worker.enqueue(EVENT_ID)
    await settle()
    assert publisher.calls == []
    clock.value = 0.5
    assert worker.enqueue(EVENT_ID)
    await settle()
    assert publisher.calls == [(EVENT_ID, ("operator",), "participant_answer")]


@pytest.mark.asyncio
async def test_enqueue_during_publish_survives_for_next_window(
    notifications: tuple[AnswerNotificationCoalescer, Publisher, Clock],
) -> None:
    worker, publisher, clock = notifications
    publisher.gate = asyncio.Event()
    worker.enqueue(EVENT_ID)
    clock.value = 0.5
    worker.enqueue(EVENT_ID)
    await asyncio.wait_for(publisher.entered.wait(), timeout=0.1)
    clock.value = 0.6
    worker.enqueue(EVENT_ID)
    publisher.gate.set()
    await settle()
    assert len(publisher.calls) == 1
    clock.value = 1.0
    worker.enqueue(EVENT_ID)
    await settle()
    assert len(publisher.calls) == 1
    clock.value = 1.101
    worker.enqueue(EVENT_ID)
    await settle()
    assert len(publisher.calls) == 2


@pytest.mark.asyncio
async def test_failure_is_one_attempt_per_batch_and_later_enqueue_recovers(
    notifications: tuple[AnswerNotificationCoalescer, Publisher, Clock],
) -> None:
    worker, publisher, clock = notifications
    publisher.failure = RedisError("fixture")
    worker.enqueue(EVENT_ID)
    clock.value = 0.5
    worker.enqueue(EVENT_ID)
    await settle()
    assert len(publisher.calls) == 1
    await settle()
    assert len(publisher.calls) == 1
    publisher.failure = None
    worker.enqueue(EVENT_ID)
    clock.value = 1.0
    worker.enqueue(EVENT_ID)
    await settle()
    assert len(publisher.calls) == 2


@pytest.mark.asyncio
async def test_timeout_does_not_stop_worker_or_leave_publish_task() -> None:
    publisher = Publisher()
    publisher.gate = asyncio.Event()
    worker = AnswerNotificationCoalescer(
        publisher, operation_timeout_seconds=0.01, window_seconds=0.001
    )
    await worker.start()
    try:
        assert worker.enqueue(EVENT_ID)
        await asyncio.wait_for(publisher.cancelled.wait(), timeout=0.2)
        assert len(publisher.calls) == 1
        publisher.gate.set()
        assert worker.enqueue(EVENT_ID)
        await worker.close()
        assert len(publisher.calls) == 2
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_shutdown_flushes_latest_once_and_rejects_new_enqueues(
    notifications: tuple[AnswerNotificationCoalescer, Publisher, Clock],
) -> None:
    worker, publisher, _clock = notifications
    for _ in range(100):
        worker.enqueue(EVENT_ID)
    await worker.close()
    assert publisher.calls == [(EVENT_ID, ("operator",), "participant_answer")]
    assert not worker.enqueue(EVENT_ID)
    await worker.close()
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_blocked_publish_within_finite_timeout() -> None:
    publisher = Publisher()
    publisher.gate = asyncio.Event()
    worker = AnswerNotificationCoalescer(
        publisher, operation_timeout_seconds=10, shutdown_timeout_seconds=0.01
    )
    await worker.start()
    worker.enqueue(EVENT_ID)
    await asyncio.wait_for(worker.close(), timeout=0.2)
    assert publisher.cancelled.is_set()
    assert not worker.enqueue(EVENT_ID)


@pytest.mark.asyncio
async def test_pending_storage_is_single_event_and_worker_must_be_started() -> None:
    publisher = Publisher()
    worker = AnswerNotificationCoalescer(publisher, operation_timeout_seconds=0.1)
    assert not worker.enqueue(EVENT_ID)
    await worker.start()
    try:
        assert worker.enqueue(EVENT_ID)
        assert not worker.enqueue(UUID(int=2))
        await worker.close()
        assert [call[0] for call in publisher.calls] == [EVENT_ID]
    finally:
        await worker.close()
