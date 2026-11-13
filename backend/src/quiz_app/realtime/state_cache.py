"""期限と無効化世代を持つ、process内の共通projection cache。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Condition
from time import monotonic
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlmodel import Session

from quiz_app.domain.errors import DomainInvariantError

Snapshot = TypeVar("Snapshot", bound=BaseModel)
COMMON_STATE_TTL_SECONDS = 5.0
COMMON_STATE_LOAD_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot(Generic[Snapshot]):
    """外部DTOに混ぜず、DB時計標本の単調時刻と配信期限を保持する。"""

    state: Snapshot
    sampled_from: float
    valid_until: float


def projection_validity_seconds(
    projected_at: datetime, boundaries: tuple[datetime | None, ...]
) -> float:
    """DB時刻から導出した状態は、次の時間境界を越えて再利用しない。"""

    remaining = [
        (boundary - projected_at).total_seconds()
        for boundary in boundaries
        if boundary is not None and boundary > projected_at
    ]
    return min(COMMON_STATE_TTL_SECONDS, *remaining) if remaining else COMMON_STATE_TTL_SECONDS


class CommonStateBuildExpiredError(DomainInvariantError):
    """構築中に共通snapshotの最大保持時間を超えた。"""


class CommonStateRefreshRequired(Exception):
    """読取lockを解放してから共通snapshotを準備し直す。"""

    def __init__(self, event_id: UUID) -> None:
        super().__init__("common_state_refresh_required")
        self.event_id = event_id


def configure_projection_read_timeout(session: Session) -> None:
    """専用read transactionだけを1秒で打ち切り、後続接続へ設定を残さない。"""

    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            select(
                func.set_config("statement_timeout", "1000", True),
                func.set_config("lock_timeout", "1000", True),
            )
        )


@dataclass(slots=True)
class CachedSnapshot(Generic[Snapshot]):
    generation: int = 0
    loading: bool = False
    value: Snapshot | None = None
    valid_until: float = 0.0
    version: int | None = None
    sampled_from: float = 0.0


class CommonStateCache(Generic[Snapshot]):
    """同時missを一つの構築へ束ね、更新中の無効化を捨てない。"""

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._condition = Condition()
        self._entries: dict[UUID, CachedSnapshot[Snapshot]] = {}
        self._closed = False

    def invalidate(self, event_id: UUID) -> None:
        with self._condition:
            entry = self._entries.setdefault(event_id, CachedSnapshot())
            entry.generation += 1
            entry.valid_until = 0.0
            self._condition.notify_all()

    def peek(self, event_id: UUID, expected_version: int) -> Snapshot | None:
        """callerがEvent lockを持つ場合、他builderの完了を待たない。"""

        with self._condition:
            entry = self._entries.get(event_id)
            if entry is not None and entry.value is not None and self._clock() < entry.valid_until:
                if entry.version == expected_version:
                    return entry.value
                # DBのEvent versionを先に観測したHTTP要求も、古いcacheを無効化する。
                entry.generation += 1
                entry.valid_until = 0.0
                self._condition.notify_all()
            return None

    def clear(self) -> None:
        with self._condition:
            for entry in self._entries.values():
                entry.generation += 1
                entry.valid_until = 0.0
                entry.value = None
            self._condition.notify_all()

    def close(self) -> None:
        """進行中builderがshutdown後に次世代を再構築するのを防ぐ。"""

        with self._condition:
            self._closed = True
            self.clear()

    def get_snapshot(
        self,
        event_id: UUID,
        load: Callable[[], ProjectionSnapshot[Snapshot]],
        version_of: Callable[[Snapshot], int],
        *,
        expected_version: int | None = None,
    ) -> ProjectionSnapshot[Snapshot]:
        """取得待ちは5秒、標本の鮮度もDB時計取得から最大5秒として別々に扱う。"""

        wait_until = self._clock() + COMMON_STATE_LOAD_TIMEOUT_SECONDS
        while True:
            with self._condition:
                if self._closed:
                    raise CommonStateBuildExpiredError("common_state_cache_closed")
                remaining = wait_until - self._clock()
                if remaining <= 0:
                    raise CommonStateBuildExpiredError("common_state_load_wait_expired")
                entry = self._entries.setdefault(event_id, CachedSnapshot())
                if (
                    entry.value is not None
                    and self._clock() < entry.valid_until
                    and (expected_version is None or entry.version == expected_version)
                ):
                    return ProjectionSnapshot(entry.value, entry.sampled_from, entry.valid_until)
                if entry.loading:
                    if not self._condition.wait(remaining):
                        raise CommonStateBuildExpiredError("common_state_wait_expired")
                    continue
                entry.loading = True
                generation = entry.generation
            snapshot: ProjectionSnapshot[Snapshot] | None = None
            try:
                loaded = load()
                if self._clock() < min(loaded.valid_until, wait_until):
                    snapshot = loaded
            finally:
                with self._condition:
                    # 成功値の公開とloading解除は同じlock内で行う。
                    if (
                        snapshot is not None
                        and not self._closed
                        and generation == entry.generation
                        and (entry.version is None or version_of(snapshot.state) >= entry.version)
                    ):
                        entry.value = snapshot.state
                        entry.version = version_of(snapshot.state)
                        entry.sampled_from = snapshot.sampled_from
                        entry.valid_until = snapshot.valid_until
                    entry.loading = False
                    self._condition.notify_all()
                    closed = self._closed
                    unchanged = generation == entry.generation
            if closed:
                raise CommonStateBuildExpiredError("common_state_cache_closed")
            if unchanged and snapshot is not None:
                return loaded
            # join等の世代変更と構築中の標本失効は、同じ有界待機内で最新値を再構築する。


class CommonStatePreparation(Generic[Snapshot]):
    """HTTP missの準備を共有し、timeout後も同じthreadの完了を待つ。"""

    def __init__(self, load: Callable[[UUID], Snapshot]) -> None:
        self._load = load
        self._tasks: dict[tuple[asyncio.AbstractEventLoop, UUID], asyncio.Task[Snapshot]] = {}
        self._closed = False

    async def prepare(self, event_id: UUID) -> Snapshot:
        if self._closed:
            raise CommonStateBuildExpiredError("common_state_cache_closed")
        key = (asyncio.get_running_loop(), event_id)
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                asyncio.to_thread(self._load, event_id), name="http-common-state-builder"
            )
            self._tasks[key] = task

            def finished(completed: asyncio.Task[Snapshot]) -> None:
                if self._tasks.get(key) is completed:
                    del self._tasks[key]
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        async with asyncio.timeout(COMMON_STATE_LOAD_TIMEOUT_SECONDS):
            return await asyncio.shield(task)

    async def close(self) -> None:
        self._closed = True
        pending = tuple(
            task for (loop, _), task in self._tasks.items() if loop is asyncio.get_running_loop()
        )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
