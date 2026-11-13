"""参加者回答経路を、非識別のserver側時間に分解する。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

TimingStage = Literal[
    "api_total",
    "pre_handler",
    "participant_metadata",
    "recovery_wait",
    "common_state_wait",
    "thread_wait",
    "dispatch_wait",
    "handler",
    "db_acquire",
    "db_lock_query",
    "answer_transaction",
    "state_projection",
    "status_projection",
    "redis_publish",
    "response_build",
]
ProjectionStage = Literal["state_projection", "status_projection"]
DatabaseWorkKind = Literal["statements", "acquisitions", "auth_cache_hits", "sessions"]
AnswerDifferenceBand = Literal[
    "below_minus_2500",
    "minus_2500_to_minus_251",
    "minus_250_to_minus_1",
    "0_to_250",
    "251_to_1000",
    "1001_to_2500",
    "2501_to_4000",
    "4001_to_10000",
    "10001_to_14000",
    "above_14000",
]


@dataclass
class RequestTiming:
    """Contextのcopy先workerも同じ集計を参照し、識別子を保持しない。"""

    started_at: float = field(default_factory=monotonic)
    durations: dict[TimingStage, float] = field(default_factory=dict)
    database_work: dict[DatabaseWorkKind, int] = field(default_factory=dict)
    answer_difference_band: AnswerDifferenceBand | None = None
    handler_dispatched_at: float | None = None
    handler_finished_at: float | None = None
    _finished_at: float | None = None

    def add(self, stage: TimingStage, duration_seconds: float) -> None:
        # cancel後もsync workerが続く場合、終了済みrequestへ遅い計測を混ぜない。
        if self._finished_at is not None:
            return
        self.durations[stage] = self.durations.get(stage, 0.0) + max(0.0, duration_seconds)

    def count_database_work(self, kind: DatabaseWorkKind) -> None:
        if self._finished_at is None:
            self.database_work[kind] = self.database_work.get(kind, 0) + 1

    def finish_response(self) -> None:
        if self._finished_at is not None:
            return
        now = monotonic()
        self.add("api_total", now - self.started_at)
        if self.handler_finished_at is not None:
            self.add("response_build", now - self.handler_finished_at)
        self._finished_at = now

    def server_timing(self) -> bytes:
        return ", ".join(
            f"{stage};dur={seconds * 1000:.3f}" for stage, seconds in self.durations.items()
        ).encode("ascii")


current_request_timing: ContextVar[RequestTiming | None] = ContextVar(
    "participant_request_timing", default=None
)


def record_timing(stage: TimingStage, duration_seconds: float) -> None:
    timing = current_request_timing.get()
    if timing is not None:
        timing.add(stage, duration_seconds)


def record_database_work(kind: DatabaseWorkKind) -> None:
    """識別子やSQL本文を保持せず、request内のDB仕事量を数える。"""

    timing = current_request_timing.get()
    if timing is not None:
        timing.count_database_work(kind)


def record_answer_difference(difference_ms: int) -> None:
    """生の回答時間や識別子を保持せず、採用境界に対応した固定帯だけを集計する。"""

    timing = current_request_timing.get()
    if timing is None:
        return
    if difference_ms < -2500:
        band: AnswerDifferenceBand = "below_minus_2500"
    elif difference_ms < -250:
        band = "minus_2500_to_minus_251"
    elif difference_ms < 0:
        band = "minus_250_to_minus_1"
    elif difference_ms <= 250:
        band = "0_to_250"
    elif difference_ms <= 1000:
        band = "251_to_1000"
    elif difference_ms <= 2500:
        band = "1001_to_2500"
    elif difference_ms <= 4000:
        band = "2501_to_4000"
    elif difference_ms <= 10000:
        band = "4001_to_10000"
    elif difference_ms <= 14000:
        band = "10001_to_14000"
    else:
        band = "above_14000"
    timing.answer_difference_band = band


@contextmanager
def observe_stage(stage: TimingStage) -> Iterator[None]:
    """補助内訳は重複を許す。lock queryを純粋なlock待ちと解釈しない。"""

    started_at = monotonic()
    try:
        yield
    finally:
        record_timing(stage, monotonic() - started_at)


async def mark_handler_dispatch() -> None:
    """末尾dependencyからendpointへ渡る前の待ちと処理を記録する。"""

    timing = current_request_timing.get()
    if timing is not None:
        timing.handler_dispatched_at = monotonic()
        timing.add("pre_handler", timing.handler_dispatched_at - timing.started_at)


@contextmanager
def observe_handler(
    projection_stage: ProjectionStage | None = None,
    *,
    is_async: bool = False,
) -> Iterator[None]:
    """asyncのdispatch差と同期workerへの待ちを区別し、handler全体を測る。"""

    timing = current_request_timing.get()
    started_at = monotonic()
    if timing is not None and timing.handler_dispatched_at is not None:
        timing.add(
            "dispatch_wait" if is_async else "thread_wait",
            started_at - timing.handler_dispatched_at,
        )
    try:
        yield
    finally:
        finished_at = monotonic()
        if timing is not None:
            timing.add("handler", finished_at - started_at)
            if projection_stage is not None:
                timing.add(projection_stage, finished_at - started_at)
            timing.handler_finished_at = finished_at
