"""commit済み回答の運営通知を固定窓へ集約する。"""

import asyncio
from collections.abc import Callable
from time import monotonic
from uuid import UUID

import logfire
from fastapi import Request
from redis.exceptions import RedisError

from quiz_app.observability.realtime_failures import log_realtime_failure
from quiz_app.realtime.redis_adapter import OPERATOR_AUDIENCE, AsyncRealtimePublisher


class AnswerNotificationCoalescer:
    """単一開催のdirty状態だけを保持し、回答ごとのtaskを作らない。"""

    def __init__(
        self,
        publisher: AsyncRealtimePublisher,
        *,
        operation_timeout_seconds: float,
        window_seconds: float = 0.5,
        shutdown_timeout_seconds: float = 1.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if min(operation_timeout_seconds, window_seconds, shutdown_timeout_seconds) <= 0:
            raise ValueError("answer notification durations must be positive")
        self._publisher = publisher
        self._operation_timeout = operation_timeout_seconds
        self._window = window_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._clock = clock
        self._event_id: UUID | None = None
        self._deadline: float | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("answer notification worker already started")
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="answer-notification-publisher")

    def enqueue(self, event_id: UUID) -> bool:
        """通知を同期で預かる。失敗してもcommit済み回答を拒否しない。"""

        if self._closing or self._task is None or self._task.done():
            logfire.warning("answer notification worker unavailable")
            return False
        if self._event_id is not None and self._event_id != event_id:
            logfire.error("answer notification single event mismatch")
            return False
        self._event_id = event_id
        if self._deadline is None:
            # 後続回答で期限を延長しない。publish中の回答は次の窓へ残す。
            self._deadline = self._clock() + self._window
        self._wake.set()
        return True

    async def _publish_pending(self) -> None:
        event_id = self._event_id
        self._deadline = None
        if event_id is None:
            return
        try:
            async with asyncio.timeout(self._operation_timeout):
                await self._publisher.notify(
                    event_id, OPERATOR_AUDIENCE, cause="participant_answer"
                )
        except (RedisError, OSError, TimeoutError) as error:
            # 1 batchは1 attemptで打ち切り、次の通知・再接続GETで回復する。
            log_realtime_failure(error, "answer_notification", audience="operator")

    async def _run(self) -> None:
        while True:
            if self._closing:
                if self._deadline is not None:
                    await self._publish_pending()
                return
            if self._deadline is None:
                await self._wake.wait()
                self._wake.clear()
                continue
            delay = self._deadline - self._clock()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self._wake.clear()
                continue
            await self._publish_pending()

    async def close(self) -> None:
        """最新dirtyだけを有限時間でflushし、Redisを閉じる前にtaskを回収する。"""

        self._closing = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await asyncio.shield(task)
        except TimeoutError:
            logfire.warning("answer notification shutdown flush timed out")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
            self._event_id = None
            self._deadline = None


async def get_answer_notifications(request: Request) -> AnswerNotificationCoalescer:
    notifications = request.app.state.answer_notifications
    if not isinstance(notifications, AnswerNotificationCoalescer):
        raise RuntimeError("answer notifications not configured")
    return notifications
