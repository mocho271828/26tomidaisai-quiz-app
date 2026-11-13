"""負荷試験と通常運用で使う非識別のprocess計測。"""

import asyncio
from time import monotonic

from anyio.to_thread import current_default_thread_limiter
from sqlalchemy.pool import PoolProxiedConnection, QueuePool

from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.request_timing import record_database_work, record_timing


class ObservedQueuePool(QueuePool):
    """取得開始を公開Pool.connect境界で測る（純粋なqueue待ちとは区別する）。"""

    quiz_metrics: QuizMetrics | None = None

    def connect(self) -> PoolProxiedConnection:
        started = monotonic()
        record_database_work("acquisitions")
        succeeded = False
        try:
            connection = super().connect()
            succeeded = True
            return connection
        finally:
            elapsed = monotonic() - started
            record_timing("db_acquire", elapsed)
            if self.quiz_metrics is not None:
                self.quiz_metrics.database_acquisition.labels(
                    result="success" if succeeded else "error"
                ).observe(elapsed)


async def monitor_runtime(metrics: QuizMetrics, stop: asyncio.Event) -> None:
    """単調時計のloop遅延とthread limiterを1秒ごとに集約する。"""

    interval_seconds = 1.0
    limiter = current_default_thread_limiter()
    while not stop.is_set():
        scheduled = monotonic() + interval_seconds
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            metrics.event_loop_delay.observe(max(0.0, monotonic() - scheduled))
            statistics = limiter.statistics()
            metrics.thread_tokens.labels(state="borrowed").set(statistics.borrowed_tokens)
            metrics.thread_tokens.labels(state="total").set(statistics.total_tokens)
            metrics.thread_tokens.labels(state="waiting").set(statistics.tasks_waiting)
