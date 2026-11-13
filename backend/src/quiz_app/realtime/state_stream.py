"""Redis通知から共通snapshotを一度だけ構築し、WSへ配信する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from secrets import SystemRandom
from time import monotonic
from uuid import UUID

from fastapi import WebSocket
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.audience.contracts import ParticipantCommonStateResponse
from quiz_app.audience.projection import AudienceProjectionService, get_audience_projection_service
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.models.enums import EventState, RoundState
from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.realtime_failures import log_realtime_failure
from quiz_app.operator.control_contracts import OperatorStateResponse
from quiz_app.operator.projection import OperatorProjectionService, get_operator_projection_service
from quiz_app.realtime.contracts import InvalidationCause, RealtimeAudience
from quiz_app.realtime.projection_budget import ProjectionReadDeferred
from quiz_app.realtime.redis_adapter import (
    RealtimeSubscription,
    RealtimeSubscriptionFactory,
    RealtimeUnavailableError,
)
from quiz_app.realtime.state_cache import COMMON_STATE_LOAD_TIMEOUT_SECONDS, ProjectionSnapshot

SharedState = ParticipantCommonStateResponse | OperatorStateResponse
SharedSnapshot = ProjectionSnapshot[SharedState]
RECONCILIATION_SECONDS = 5.0
ANSWER_UPDATE_SECONDS = 0.5
PROGRESSION_RECONCILIATION_SECONDS = 1.0
DB_RETRY_SECONDS = 5.0
DB_RETRY_MAX_SECONDS = 30.0


class StateSnapshotRefreshRequired(Exception):
    """失効した標本を配信せず、heartbeatを継続して次の標本を待つ。"""


@dataclass(eq=False, slots=True)
class StateHub:
    audience: RealtimeAudience
    event_id: UUID
    source: RealtimeSubscription | None
    snapshot: SharedSnapshot
    listeners: set[StateSubscription] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    refresh_task: asyncio.Task[bool] | None = None
    failed: bool = False
    reader_task: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    updated: asyncio.Event = field(default_factory=asyncio.Event)
    generation: int = 0
    urgent: bool = False
    normal_at: float | None = None
    last_normal_update: float = float("-inf")
    retry_at: float = 0.0
    failures: int = 0


class StateSubscription:
    """遅いWSへ中間snapshotを蓄積せず、最新一件だけ渡す。"""

    def __init__(self, factory: StateStreamFactory, hub: StateHub) -> None:
        self._factory = factory
        self._hub = hub
        self._pending: asyncio.Queue[SharedState | None] = asyncio.Queue(maxsize=1)
        self._closed = False

    def deliver(self, state: SharedState | None) -> None:
        if self._pending.full():
            self._pending.get_nowait()
        self._pending.put_nowait(state)

    async def next_message(self) -> SharedState:
        if self._closed:
            raise RealtimeUnavailableError("state_subscription_closed")
        state = await self._pending.get()
        if state is None:
            raise RealtimeUnavailableError("state_snapshot_unavailable")
        try:
            return self.snapshot_for_send()[0]
        except StateSnapshotRefreshRequired:
            return await self.fresh_message()

    async def fresh_message(self) -> SharedState:
        if self._closed or self._hub.failed:
            raise RealtimeUnavailableError("state_snapshot_unavailable")
        while not self._closed and not self._hub.failed:
            updated = self._hub.updated
            try:
                return self.snapshot_for_send()[0]
            except StateSnapshotRefreshRequired:
                # DB障害でも接続ごとの再試行を作らず、共有schedulerだけに照合を任せる。
                self._hub.wake.set()
                await updated.wait()
        raise RealtimeUnavailableError("state_snapshot_unavailable")

    def snapshot_for_send(self) -> tuple[SharedState, float]:
        """認証待機後にも最新値を選び、送信中の期限を呼出元へ渡す。"""

        deadline = self._factory._refresh_at(self._hub)
        if self._hub.failed:
            raise RealtimeUnavailableError("state_snapshot_unavailable")
        if self._hub.urgent or self._factory.current_time() >= deadline:
            raise StateSnapshotRefreshRequired("state_snapshot_expired")
        return self._hub.snapshot.state, deadline

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup = asyncio.create_task(self._factory.release(self, self._hub))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def same_content(left: SharedState, right: SharedState) -> bool:
    return left.model_dump(exclude={"projected_at"}) == right.model_dump(exclude={"projected_at"})


class StateStreamFactory:
    """process・開催・audience単位で購読とsnapshot更新を共有する。"""

    def __init__(
        self,
        subscriptions: RealtimeSubscriptionFactory,
        audience_projection: AudienceProjectionService | None = None,
        operator_projection: OperatorProjectionService | None = None,
        clock: Callable[[], float] = monotonic,
        metrics: QuizMetrics | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._audience_projection = audience_projection
        self._operator_projection = operator_projection
        self._clock = clock
        self._metrics = metrics
        self._hubs: dict[tuple[RealtimeAudience, UUID], StateHub] = {}
        self._loads: dict[tuple[RealtimeAudience, UUID], asyncio.Task[SharedSnapshot]] = {}
        self._lock = asyncio.Lock()
        self._subscription_locks: dict[tuple[RealtimeAudience, UUID], asyncio.Lock] = {}
        self._closed = False

    async def start(self) -> None:
        self._closed = False

    def current_time(self) -> float:
        return self._clock()

    def _reconciliation_interval(self, hub: StateHub) -> float:
        current = hub.snapshot.state.current_round
        preparing = current is not None and current.state in {
            RoundState.PREPARING_ASSETS,
            RoundState.READY_TO_SCHEDULE,
            RoundState.SCHEDULED,
        }
        event_state = (
            hub.snapshot.state.event.event_state
            if isinstance(hub.snapshot.state, ParticipantCommonStateResponse)
            else hub.snapshot.state.event_state
        )
        degraded_active = hub.source is None and event_state is EventState.IN_PROGRESS
        return (
            PROGRESSION_RECONCILIATION_SECONDS
            if preparing or degraded_active
            else RECONCILIATION_SECONDS
        )

    def _refresh_at(self, hub: StateHub) -> float:
        return min(
            hub.snapshot.valid_until,
            hub.snapshot.sampled_from + self._reconciliation_interval(hub),
        )

    def _back_off(self, hub: StateHub) -> None:
        hub.failures += 1
        hub.retry_at = self._clock() + min(
            DB_RETRY_MAX_SECONDS, DB_RETRY_SECONDS * 2 ** min(hub.failures - 1, 3)
        )
        self._defer_results(hub, True)

    def _defer_results(self, hub: StateHub, deferred: bool) -> None:
        service = self._audience_projection
        if service is None:
            service = get_audience_projection_service()
        # 簡易projectionを使う単体試験でも外部DBを起動しない。
        personal = getattr(service, "personal_results", None)
        if personal is not None:
            personal.set_deferred(hub.event_id, hub.audience, deferred)

    def invalidate(
        self, event_id: UUID, audiences: Sequence[RealtimeAudience], cause: InvalidationCause
    ) -> None:
        """loop上で通知だけを預かり、DB構築待ちを運営HTTPへ持ち込まない。"""

        for audience in audiences:
            hub = self._hubs.get((audience, event_id))
            if hub is None:
                continue
            if cause in {"participant_answer", "participant_join"}:
                if hub.normal_at is None:
                    hub.normal_at = max(
                        self._clock(), hub.last_normal_update + ANSWER_UPDATE_SECONDS
                    )
            else:
                hub.generation += 1
                hub.urgent = True
                hub.normal_at = None
                self._defer_results(hub, True)
            hub.wake.set()

    async def _listen(self, hub: StateHub) -> None:
        retries = 0
        while True:
            if hub.source is None:
                delay = min(30.0, 1.0 * 2 ** min(retries, 5))
                await asyncio.sleep(SystemRandom().uniform(delay, min(30.0, delay * 1.2)))
                try:
                    hub.source = await self._subscriptions.subscribe(hub.audience, hub.event_id)
                except RealtimeUnavailableError:
                    retries += 1
                    continue
                retries = 0
                # 再購読前に失った通知を最新DBとの照合で埋める。
                self.invalidate(hub.event_id, (hub.audience,), "operator_mutation")
            try:
                message = await hub.source.next_message()
            except RealtimeUnavailableError as error:
                log_realtime_failure(error, "shared_state", audience=hub.audience)
                await hub.source.close()
                hub.source = None
                if self._metrics is not None:
                    self._metrics.set_realtime_redis_degraded(
                        audience=hub.audience, is_degraded=True
                    )
                hub.wake.set()
            else:
                self.invalidate(message.event_id, (message.audience,), message.cause)

    def _load(
        self, audience: RealtimeAudience, event_id: UUID, *, normal: bool = False
    ) -> SharedSnapshot:
        if audience == "participant":
            service = self._audience_projection or get_audience_projection_service()
            service.invalidate_common_state(event_id)
            return (
                service.get_common_snapshot(event_id, normal=True)
                if normal
                else service.get_common_snapshot(event_id)
            )
        operator = self._operator_projection or get_operator_projection_service()
        operator.invalidate_common_state(event_id)
        return (
            operator.get_event_snapshot(event_id, normal=True)
            if normal
            else operator.get_event_snapshot(event_id)
        )

    async def _load_once(
        self, audience: RealtimeAudience, event_id: UUID, *, normal: bool = False
    ) -> SharedSnapshot:
        key = (audience, event_id)
        async with asyncio.timeout(COMMON_STATE_LOAD_TIMEOUT_SECONDS):
            while True:
                task = self._loads.get(key)
                if task is None:
                    task = asyncio.create_task(
                        asyncio.to_thread(self._load, audience, event_id, normal=normal),
                        name="shared-state-builder",
                    )
                    self._loads[key] = task

                    def finished(completed: asyncio.Task[SharedSnapshot]) -> None:
                        # await timeoutはthread本体を止めない。終了まで同じTaskを共有する。
                        if self._loads.get(key) is completed:
                            del self._loads[key]
                        if not completed.cancelled():
                            completed.exception()

                    task.add_done_callback(finished)
                snapshot = await asyncio.shield(task)
                if self._clock() < snapshot.valid_until:
                    return snapshot
                # done callbackの実行前でも、失効済みTaskを再awaitしてbusy loopにしない。
                if self._loads.get(key) is task:
                    del self._loads[key]

    async def subscribe(self, audience: RealtimeAudience, event_id: UUID) -> StateSubscription:
        try:
            async with asyncio.timeout(COMMON_STATE_LOAD_TIMEOUT_SECONDS):
                return await self._subscribe(audience, event_id)
        except TimeoutError as error:
            raise RealtimeUnavailableError("state_snapshot_unavailable") from error

    async def _subscribe(self, audience: RealtimeAudience, event_id: UUID) -> StateSubscription:
        async with self._subscription_locks.setdefault((audience, event_id), asyncio.Lock()):
            if self._closed:
                raise RealtimeUnavailableError("state_factory_closed")
            key = (audience, event_id)
            hub = self._hubs.get(key)
            if hub is not None and hub.failed:
                await self._stop(hub)
                del self._hubs[key]
                hub = None
            if hub is None:
                # 購読を先に成立させ、初回snapshotの構築中の更新もqueueへ残す。
                try:
                    source = await self._subscriptions.subscribe(audience, event_id)
                except RealtimeUnavailableError as error:
                    log_realtime_failure(error, "shared_state", audience=audience)
                    source = None
                    if self._metrics is not None:
                        self._metrics.set_realtime_redis_degraded(
                            audience=audience, is_degraded=True
                        )
                try:
                    snapshot = await self._load_once(audience, event_id)
                except (
                    RealtimeUnavailableError,
                    SQLAlchemyError,
                    DomainInvariantError,
                    ResourceNotFoundError,
                    TimeoutError,
                ) as error:
                    if source is not None:
                        await source.close()
                    raise RealtimeUnavailableError("state_snapshot_unavailable") from error
                except asyncio.CancelledError:
                    if source is not None:
                        await source.close()
                    raise
                if self._closed:
                    if source is not None:
                        await source.close()
                    raise RealtimeUnavailableError("state_factory_closed")
                hub = StateHub(audience, event_id, source, snapshot)
                self._hubs[key] = hub
                hub.task = asyncio.create_task(self._run(hub), name="shared-state-stream")
                hub.reader_task = asyncio.create_task(
                    self._listen(hub), name="shared-state-notifications"
                )
            subscription = StateSubscription(self, hub)
            hub.listeners.add(subscription)
            subscription.deliver(hub.snapshot.state)
            return subscription

    async def _refresh(self, hub: StateHub, *, normal: bool = False) -> bool:
        task = hub.refresh_task
        if task is None:
            task = asyncio.create_task(
                self._refresh_snapshot(hub, normal=normal), name="shared-state-refresh"
            )
            hub.refresh_task = task

            def finished(completed: asyncio.Task[bool]) -> None:
                if hub.refresh_task is completed:
                    hub.refresh_task = None
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _refresh_snapshot(self, hub: StateHub, *, normal: bool = False) -> bool:
        generation = hub.generation
        snapshot = await self._load_once(hub.audience, hub.event_id, normal=normal)
        if generation != hub.generation:
            if self._metrics is not None:
                self._metrics.record_realtime_build(
                    audience=hub.audience, kind="common", outcome="discarded"
                )
            return False
        changed = not same_content(hub.snapshot.state, snapshot.state)
        hub.snapshot = snapshot
        hub.urgent = False
        if changed:
            for listener in tuple(hub.listeners):
                listener.deliver(snapshot.state)
        updated, hub.updated = hub.updated, asyncio.Event()
        updated.set()
        return changed

    async def _run(self, hub: StateHub) -> None:
        build: asyncio.Task[bool] | None = None
        wake = asyncio.create_task(hub.wake.wait())
        started = self._clock()
        try:
            while True:
                now = self._clock()
                deadline = self._refresh_at(hub)
                if hub.urgent:
                    deadline = now
                elif hub.normal_at is not None:
                    deadline = min(deadline, hub.normal_at)
                deadline = max(deadline, hub.retry_at)
                if build is None and now >= deadline:
                    normal = (
                        not hub.urgent and hub.normal_at is not None and now < self._refresh_at(hub)
                    )
                    hub.normal_at = None
                    started = now
                    self._defer_results(hub, True)
                    build = asyncio.create_task(self._refresh(hub, normal=normal))
                waiting = {wake}
                if build is not None:
                    waiting.add(build)
                done, _ = await asyncio.wait(
                    waiting,
                    timeout=None if build is not None else max(0.0, deadline - now),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wake in done:
                    hub.wake.clear()
                    wake = asyncio.create_task(hub.wake.wait())
                if build is not None and build in done:
                    try:
                        changed = build.result()
                    except ProjectionReadDeferred:
                        # 通常枠の満杯はDB障害ではない。進行通知が来れば待機を追い越す。
                        hub.normal_at = self._clock() + ANSWER_UPDATE_SECONDS
                        self._defer_results(hub, hub.urgent)
                        if self._metrics is not None:
                            self._metrics.record_realtime_build(
                                audience=hub.audience, kind="common", outcome="deferred"
                            )
                    except (
                        SQLAlchemyError,
                        DomainInvariantError,
                        ResourceNotFoundError,
                        TimeoutError,
                    ) as error:
                        log_realtime_failure(error, "shared_state", audience=hub.audience)
                        # Redis単独障害とDB障害を分離し、DB遅延中は照合を増やさない。
                        self._back_off(hub)
                        if self._metrics is not None:
                            self._metrics.record_realtime_build(
                                audience=hub.audience,
                                kind="common",
                                outcome="timeout" if isinstance(error, TimeoutError) else "error",
                                duration_seconds=self._clock() - started,
                            )
                    else:
                        elapsed = self._clock() - started
                        if elapsed >= self._reconciliation_interval(hub):
                            self._back_off(hub)
                        else:
                            hub.failures = 0
                            hub.retry_at = 0.0
                            self._defer_results(hub, hub.urgent)
                        if self._metrics is not None and hub.source is not None and not hub.urgent:
                            self._metrics.set_realtime_redis_degraded(
                                audience=hub.audience, is_degraded=False
                            )
                        if changed:
                            hub.last_normal_update = self._clock()
                        if self._metrics is not None:
                            self._metrics.record_realtime_build(
                                audience=hub.audience,
                                kind="common",
                                outcome="success",
                                duration_seconds=self._clock() - started,
                            )
                    build = None
        finally:
            wake.cancel()
            if build is not None:
                build.cancel()
            await asyncio.gather(
                wake, *([build] if build is not None else []), return_exceptions=True
            )

    async def release(self, subscription: StateSubscription, hub: StateHub) -> None:
        async with self._lock:
            hub.listeners.discard(subscription)
            key = (hub.audience, hub.event_id)
            if not hub.listeners and self._hubs.get(key) is hub:
                del self._hubs[key]
                await self._stop(hub)

    async def _stop(self, hub: StateHub) -> None:
        hub.failed = True
        hub.updated.set()
        self._defer_results(hub, False)
        if hub.reader_task is not None:
            hub.reader_task.cancel()
            await asyncio.gather(hub.reader_task, return_exceptions=True)
        if hub.task is not None:
            hub.task.cancel()
            await asyncio.gather(hub.task, return_exceptions=True)
        if hub.refresh_task is not None:
            hub.refresh_task.cancel()
            await asyncio.gather(hub.refresh_task, return_exceptions=True)
        if hub.source is not None:
            await hub.source.close()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for hub in self._hubs.values():
                for listener in tuple(hub.listeners):
                    listener.deliver(None)
                await self._stop(hub)
            self._hubs.clear()
            audience = self._audience_projection or get_audience_projection_service()
            operator = self._operator_projection or get_operator_projection_service()
            # cacheを先に閉じ、進行中buildが無効化後にSQLを再実行するのを防ぐ。
            await operator.close_common_cache()
            await audience.close_common_cache()
            if self._loads:
                # driver側で有限timeoutを持つ残りのbuilderを回収してからEngineを閉じる。
                await asyncio.gather(*tuple(self._loads.values()), return_exceptions=True)
            if self._audience_projection is None:
                get_audience_projection_service.cache_clear()
            if self._operator_projection is None:
                get_operator_projection_service.cache_clear()


def get_state_stream_factory(websocket: WebSocket) -> StateStreamFactory:
    return websocket.app.state.realtime_state_factory
