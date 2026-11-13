"""commit済み通知を有限dirtyへ預かり、HTTP応答をRedis I/Oから分離する。"""

import asyncio
from collections.abc import Sequence
from time import monotonic
from uuid import UUID

from quiz_app.observability.realtime_failures import log_realtime_failure
from quiz_app.realtime.contracts import InvalidationCause, RealtimeAudience
from quiz_app.realtime.redis_adapter import (
    AsyncRealtimePublisher,
    RealtimePublisher,
    invalidate_local_common_state,
)
from quiz_app.realtime.state_stream import StateStreamFactory


class NotificationDispatcher:
    """進行と通常通知を別workerへ分け、各audienceの最新dirtyだけ残す。"""

    def __init__(
        self, publisher: AsyncRealtimePublisher, states: StateStreamFactory, *, timeout: float
    ) -> None:
        self._publisher, self._states, self._timeout = publisher, states, timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[bool, dict[RealtimeAudience, tuple[UUID, InvalidationCause, float]]] = {
            True: {},
            False: {},
        }
        self._wake = {True: asyncio.Event(), False: asyncio.Event()}
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._tasks = [
            asyncio.create_task(self._run(urgent), name=f"notification-dispatch-{urgent}")
            for urgent in (True, False)
        ]

    def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause = "operator_mutation",
    ) -> bool:
        """同期HTTP workerからもI/Oなしでenqueueし、DB成功を通知成功と混同しない。"""

        invalidate_local_common_state(event_id, audiences)
        if self._closed or self._loop is None:
            return False
        self._loop.call_soon_threadsafe(self._enqueue, event_id, tuple(audiences), cause)
        return True

    def _enqueue(
        self, event_id: UUID, audiences: Sequence[RealtimeAudience], cause: InvalidationCause
    ) -> None:
        if self._closed:
            return
        urgent = cause not in {"participant_answer", "participant_join"}
        now = monotonic()
        for audience in audiences:
            existing = self._pending[urgent].get(audience)
            # 回答は上流の固定500ms窓を通過済み。joinだけここで同じ固定窓へ集約する。
            deadline = (
                existing[2] if existing else now + (0.5 if cause == "participant_join" else 0)
            )
            if cause == "participant_answer":
                deadline = min(deadline, now)
            self._pending[urgent][audience] = (event_id, cause, deadline)
        if urgent:
            self._states.invalidate(event_id, audiences, cause)
        self._wake[urgent].set()

    async def _run(self, urgent: bool) -> None:
        pending, wake = self._pending[urgent], self._wake[urgent]
        while True:
            if not pending:
                await wake.wait()
                wake.clear()
                continue
            audience, (event_id, cause, deadline) = min(
                pending.items(), key=lambda item: item[1][2]
            )
            delay = deadline - monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(wake.wait(), delay)
                except TimeoutError:
                    pass
                wake.clear()
                continue
            del pending[audience]
            if not urgent:
                self._states.invalidate(event_id, (audience,), cause)
            try:
                async with asyncio.timeout(self._timeout):
                    await self._publisher.notify(event_id, (audience,), cause=cause)
            except TimeoutError as error:
                log_realtime_failure(error, "redis_publish", audience=audience)

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for pending in self._pending.values():
            pending.clear()


class QueuedAsyncRealtimePublisher(AsyncRealtimePublisher):
    """既存回答coalescerからも同じ有限dispatcherへ通知を渡す。"""

    def __init__(self, dispatcher: NotificationDispatcher) -> None:
        self._dispatcher = dispatcher

    async def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause = "operator_mutation",
    ) -> bool:
        return self._dispatcher.notify(event_id, audiences, cause=cause)


class QueuedRealtimePublisher(RealtimePublisher):
    """同期通知入口の構造的契約を明示する、API process用adapter。"""

    def __init__(self, dispatcher: NotificationDispatcher) -> None:
        self._dispatcher = dispatcher

    def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause = "operator_mutation",
    ) -> bool:
        return self._dispatcher.notify(event_id, audiences, cause=cause)
