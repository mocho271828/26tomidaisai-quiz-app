"""共通cacheの並行構築、期限、無効化とWS snapshot共有の回帰。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
from pydantic import BaseModel

from quiz_app.audience.contracts import ParticipantCommonStateResponse
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.contracts import StateInvalidatedMessage
from quiz_app.realtime.redis_adapter import RealtimeUnavailableError
from quiz_app.realtime.state_cache import (
    COMMON_STATE_TTL_SECONDS,
    CommonStateBuildExpiredError,
    CommonStateCache,
    CommonStatePreparation,
    ProjectionSnapshot,
    projection_validity_seconds,
)
from quiz_app.realtime.state_stream import StateStreamFactory

EVENT_ID = UUID(int=1)


class Snapshot(BaseModel):
    version: int


def snapshot(
    version: int, sampled_from: float = 10.0, lifetime: float = 1.0
) -> ProjectionSnapshot[Snapshot]:
    return ProjectionSnapshot(Snapshot(version=version), sampled_from, sampled_from + lifetime)


def test_concurrent_misses_have_one_builder() -> None:
    cache = CommonStateCache[Snapshot](clock=lambda: 10.0)
    entered, release = Event(), Event()
    calls = 0

    def load() -> ProjectionSnapshot[Snapshot]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(1)
        return snapshot(1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(cache.get_snapshot, EVENT_ID, load, lambda value: value.version)
            for _ in range(8)
        ]
        assert entered.wait(1)
        release.set()
        assert [future.result(1).state.version for future in futures] == [1] * 8
    assert calls == 1


def test_invalidation_during_build_discards_old_generation() -> None:
    cache = CommonStateCache[Snapshot](clock=lambda: 10.0)
    entered, release = Event(), Event()
    calls = 0

    def load() -> ProjectionSnapshot[Snapshot]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(1)
        return snapshot(calls)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cache.get_snapshot, EVENT_ID, load, lambda value: value.version)
        assert entered.wait(1)
        cache.invalidate(EVENT_ID)
        release.set()
        assert future.result(1).state.version == 2
    assert calls == 2


def test_expired_cache_never_falls_back_after_failed_or_slow_build() -> None:
    now = [10.0]
    cache = CommonStateCache[Snapshot](clock=lambda: now[0])
    assert (
        cache.get_snapshot(EVENT_ID, lambda: snapshot(1), lambda value: value.version).state.version
        == 1
    )
    now[0] = 11.0

    def fail() -> ProjectionSnapshot[Snapshot]:
        raise DomainInvariantError("fixture_failure")

    with pytest.raises(DomainInvariantError, match="fixture_failure"):
        cache.get_snapshot(EVENT_ID, fail, lambda value: value.version)
    assert cache.peek(EVENT_ID, 1) is None

    def slow() -> ProjectionSnapshot[Snapshot]:
        sampled_from = now[0]
        now[0] += 1.0
        return snapshot(2, sampled_from)

    with pytest.raises(CommonStateBuildExpiredError):
        cache.get_snapshot(EVENT_ID, slow, lambda value: value.version)
    assert cache.peek(EVENT_ID, 2) is None
    assert (
        cache.get_snapshot(
            EVENT_ID, lambda: snapshot(3, now[0]), lambda value: value.version
        ).state.version
        == 3
    )


def test_http_peek_does_not_wait_for_another_builder() -> None:
    cache = CommonStateCache[Snapshot](clock=lambda: 10.0)
    entered, release = Event(), Event()

    def load() -> ProjectionSnapshot[Snapshot]:
        entered.set()
        assert release.wait(1)
        return snapshot(1)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cache.get_snapshot, EVENT_ID, load, lambda value: value.version)
        assert entered.wait(1)
        assert cache.peek(EVENT_ID, 1) is None
        release.set()
        future.result(1)
        assert cache.peek(EVENT_ID, 1) is not None


def test_close_stops_invalidated_builder_without_rebuilding() -> None:
    cache = CommonStateCache[Snapshot](clock=lambda: 10.0)
    entered, release = Event(), Event()
    calls = 0

    def load() -> ProjectionSnapshot[Snapshot]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(1)
        return snapshot(1)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cache.get_snapshot, EVENT_ID, load, lambda value: value.version)
        assert entered.wait(1)
        cache.close()
        release.set()
        with pytest.raises(CommonStateBuildExpiredError, match="cache_closed"):
            future.result(1)
    assert calls == 1
    assert cache.peek(EVENT_ID, 1) is None


@pytest.mark.parametrize("boundary_seconds", [0.1, 3.0])
def test_cache_expires_at_derived_time_boundary_before_five_seconds(
    boundary_seconds: float,
) -> None:
    projected_at = datetime.now(UTC)
    lifetime = projection_validity_seconds(
        projected_at,
        (
            projected_at - timedelta(seconds=1),
            projected_at + timedelta(seconds=boundary_seconds),
            None,
        ),
    )
    assert lifetime == boundary_seconds
    now = [10.0]
    cache = CommonStateCache[Snapshot](clock=lambda: now[0])
    cache.get_snapshot(
        EVENT_ID,
        lambda: snapshot(1, now[0], lifetime),
        lambda value: value.version,
    )
    assert cache.peek(EVENT_ID, 1) is not None
    now[0] += lifetime
    assert cache.peek(EVENT_ID, 1) is None


def test_cache_reuses_snapshot_for_five_seconds_without_a_nearer_boundary() -> None:
    projected_at = datetime.now(UTC)
    lifetime = projection_validity_seconds(projected_at, (None,))
    assert lifetime == 5.0
    now = [10.0]
    cache = CommonStateCache[Snapshot](clock=lambda: now[0])
    cache.get_snapshot(EVENT_ID, lambda: snapshot(1, now[0], lifetime), lambda value: value.version)
    now[0] = 14.999
    assert cache.peek(EVENT_ID, 1) is not None
    now[0] = 15.0
    assert cache.peek(EVENT_ID, 1) is None


def test_pool_and_query_wait_before_db_sample_does_not_expire_fresh_snapshot() -> None:
    now = [10.0]
    cache = CommonStateCache[Snapshot](clock=lambda: now[0])

    def load() -> ProjectionSnapshot[Snapshot]:
        now[0] = 13.0
        return ProjectionSnapshot(Snapshot(version=1), 13.0, 14.0)

    snapshot = cache.get_snapshot(EVENT_ID, load, lambda state: state.version)
    assert snapshot.sampled_from == 13.0
    assert snapshot.valid_until == 14.0
    assert cache.peek(EVENT_ID, 1) is not None
    now[0] = 14.0
    assert cache.peek(EVENT_ID, 1) is None


def test_generation_churn_retries_beyond_three_until_bounded_deadline() -> None:
    now = [10.0]
    cache = CommonStateCache[Snapshot](clock=lambda: now[0])
    calls = 0

    def load() -> ProjectionSnapshot[Snapshot]:
        nonlocal calls
        calls += 1
        now[0] += 0.1
        if calls <= 4:
            cache.invalidate(EVENT_ID)
        return ProjectionSnapshot(Snapshot(version=calls), now[0], now[0] + 1)

    assert cache.get_snapshot(EVENT_ID, load, lambda state: state.version).state.version == 5
    assert calls == 5
    cache.invalidate(EVENT_ID)

    def changing() -> ProjectionSnapshot[Snapshot]:
        now[0] += 1
        cache.invalidate(EVENT_ID)
        return ProjectionSnapshot(Snapshot(version=6), now[0], now[0] + 1)

    with pytest.raises(CommonStateBuildExpiredError, match="load_wait_expired"):
        cache.get_snapshot(EVENT_ID, changing, lambda state: state.version)


@pytest.mark.asyncio
async def test_http_timeouts_reuse_single_preparation_until_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_cache.COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.02)
    entered, release = Event(), Event()
    calls = 0

    def load(_: UUID) -> Snapshot:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(1)
        return Snapshot(version=1)

    preparation = CommonStatePreparation(load)
    waiting = [asyncio.create_task(preparation.prepare(EVENT_ID)) for _ in range(100)]
    results = await asyncio.gather(*waiting, return_exceptions=True)
    assert entered.is_set()
    assert all(isinstance(result, TimeoutError) for result in results)
    with pytest.raises(TimeoutError):
        await preparation.prepare(EVENT_ID)
    assert calls == 1
    release.set()
    await preparation.close()
    with pytest.raises(CommonStateBuildExpiredError, match="cache_closed"):
        await preparation.prepare(EVENT_ID)


def shared_state(version: int) -> ParticipantCommonStateResponse:
    return ParticipantCommonStateResponse(
        award_ceremony=None,
        projected_at=datetime.now(UTC),
        event={
            "timing_basis": "server_received_v1",
            "arrival_grace_ms": 0,
            "clock_tolerance_ms": 0,
            "client_ahead_tolerance_ms": 0,
            "event_id": EVENT_ID,
            "event_title": "共有",
            "event_state": "LobbyOpen",
            "event_version": version,
            "start_delay_seconds": 3,
        },
        current_round=None,
        latest_result=None,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        ),
        latest_ranking_snapshot_version=None,
    )


class Source:
    def __init__(self) -> None:
        self.pending: asyncio.Queue[StateInvalidatedMessage | None] = asyncio.Queue()
        self.closed = False

    async def next_message(self) -> StateInvalidatedMessage:
        message = await self.pending.get()
        if message is None:
            raise RealtimeUnavailableError("fixture_lost")
        return message

    async def close(self) -> None:
        self.closed = True


class Sources:
    def __init__(self) -> None:
        self.source = Source()
        self.subscribed = False
        self.calls = 0

    async def subscribe(self, audience: str, event_id: UUID) -> Source:
        assert audience == "participant" and event_id == EVENT_ID
        self.subscribed = True
        self.calls += 1
        return self.source


class Projection:
    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self.calls = 0
        self.fail = False

    def invalidate_common_state(self, _: UUID) -> None:
        pass

    def get_common_state(self, _: UUID) -> ParticipantCommonStateResponse:
        assert self.sources.subscribed
        self.calls += 1
        if self.fail:
            raise DomainInvariantError("fixture_broken")
        return shared_state(self.calls)

    def get_common_snapshot(
        self, event_id: UUID, *, normal: bool = False
    ) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        state = self.get_common_state(event_id)
        sampled_from = monotonic()
        return ProjectionSnapshot(state, sampled_from, sampled_from + COMMON_STATE_TTL_SECONDS)

    def clear_common_cache(self) -> None:
        pass

    async def close_common_cache(self) -> None:
        pass


@pytest.mark.asyncio
async def test_hundred_clients_share_snapshot_and_subscribe_before_build() -> None:
    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    baseline = asyncio.all_tasks()
    subscriptions = await asyncio.gather(
        *(factory.subscribe("participant", EVENT_ID) for _ in range(100))
    )
    states = await asyncio.gather(*(item.next_message() for item in subscriptions))
    assert len({id(state) for state in states}) == 1
    assert projection.calls == sources.calls == 1
    sources.source.pending.put_nowait(
        StateInvalidatedMessage(
            type="state.invalidated",
            audience="participant",
            event_id=EVENT_ID,
            cause="operator_mutation",
        )
    )
    states = await asyncio.wait_for(
        asyncio.gather(*(item.next_message() for item in subscriptions)), 1
    )
    assert len({id(state) for state in states}) == 1
    assert states[0].event.event_version == 2
    assert projection.calls == 2
    assert sources.calls == 1
    await asyncio.gather(*(item.close() for item in subscriptions))
    await factory.close()
    assert sources.source.closed
    assert asyncio.all_tasks() <= baseline


@pytest.mark.asyncio
async def test_missing_notification_waits_five_seconds_before_shared_reconciliation() -> None:
    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscription = await factory.subscribe("participant", EVENT_ID)
    try:
        assert (await subscription.next_message()).event.event_version == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscription.next_message(), 4.8)
        assert projection.calls == 1
        assert (await asyncio.wait_for(subscription.next_message(), 1)).event.event_version == 2
        assert projection.calls == 2
    finally:
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_missing_notification_reconciles_and_database_failure_keeps_shared_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_stream.RECONCILIATION_SECONDS", 0.01)
    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscription = await factory.subscribe("participant", EVENT_ID)
    assert (await subscription.next_message()).event.event_version == 1
    assert (await asyncio.wait_for(subscription.next_message(), 1)).event.event_version == 2
    projection.fail = True
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.next_message(), 0.05)
    assert not subscription._hub.failed
    assert subscription._hub.failures == 1
    await subscription.close()
    await factory.close()


@pytest.mark.asyncio
async def test_periodic_change_starts_answer_window_but_operator_control_is_immediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_stream.RECONCILIATION_SECONDS", 0.1)
    monkeypatch.setattr("quiz_app.realtime.state_stream.ANSWER_UPDATE_SECONDS", 0.05)
    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscription = await factory.subscribe("participant", EVENT_ID)
    await subscription.next_message()
    await asyncio.wait_for(subscription.next_message(), 1)
    periodic_delivered_at = monotonic()
    await asyncio.sleep(0.01)

    def notification(cause: str) -> StateInvalidatedMessage:
        return StateInvalidatedMessage(
            type="state.invalidated",
            audience="participant",
            event_id=EVENT_ID,
            cause=cause,
        )

    sources.source.pending.put_nowait(notification("participant_answer"))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.next_message(), 0.02)
    await asyncio.wait_for(subscription.next_message(), 1)
    assert monotonic() - periodic_delivered_at >= 0.045
    control_started_at = monotonic()
    sources.source.pending.put_nowait(notification("operator_mutation"))
    await asyncio.wait_for(subscription.next_message(), 0.04)
    assert monotonic() - control_started_at < 0.05
    await subscription.close()
    await factory.close()


@pytest.mark.asyncio
async def test_shared_builder_keeps_original_start_for_late_awaiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    sources = Sources()
    sources.subscribed = True
    projection = Projection(sources)
    entered, release = Event(), Event()
    calls = 0

    def load(_: UUID) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(1)
        return ProjectionSnapshot(shared_state(1), now[0], now[0] + 1)

    monkeypatch.setattr(projection, "get_common_snapshot", load)
    factory = StateStreamFactory(sources, audience_projection=projection, clock=lambda: now[0])
    first = asyncio.create_task(factory._load_once("participant", EVENT_ID))
    assert await asyncio.to_thread(entered.wait, 1)
    now[0] = 10.9
    second = asyncio.create_task(factory._load_once("participant", EVENT_ID))
    await asyncio.sleep(0)
    now[0] = 10.95
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result
    assert first_result.sampled_from == 10.95
    assert first_result.valid_until == 11.95
    assert calls == 1
    await factory.close()


@pytest.mark.asyncio
async def test_hundred_expired_listeners_wait_for_one_fresh_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_stream.RECONCILIATION_SECONDS", 0.02)
    sources = Sources()
    projection = Projection(sources)
    load = projection.get_common_snapshot
    entered, release = Event(), Event()

    def delayed(event_id: UUID) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        if projection.calls == 1:
            entered.set()
            assert release.wait(1)
        return load(event_id)

    monkeypatch.setattr(projection, "get_common_snapshot", delayed)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscriptions = await asyncio.gather(
        *(factory.subscribe("participant", EVENT_ID) for _ in range(100))
    )
    await asyncio.gather(*(item.next_message() for item in subscriptions))
    assert await asyncio.to_thread(entered.wait, 1)
    waiting = [asyncio.create_task(item.fresh_message()) for item in subscriptions]
    await asyncio.sleep(0.01)
    assert all(not task.done() for task in waiting)
    release.set()
    states = await asyncio.gather(*waiting)
    assert len({id(state) for state in states}) == 1
    assert projection.calls == 2
    await asyncio.gather(*(item.close() for item in subscriptions))
    await factory.close()


@pytest.mark.asyncio
async def test_initial_load_timeout_reuses_worker_and_closes_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_stream.COMMON_STATE_LOAD_TIMEOUT_SECONDS", 0.02)
    sources = Sources()
    projection = Projection(sources)
    load = projection.get_common_snapshot
    entered, release = Event(), Event()
    calls = 0

    def delayed(event_id: UUID) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(1)
        return load(event_id)

    monkeypatch.setattr(projection, "get_common_snapshot", delayed)
    factory = StateStreamFactory(sources, audience_projection=projection)
    for _ in range(2):
        with pytest.raises(RealtimeUnavailableError):
            await factory.subscribe("participant", EVENT_ID)
    assert entered.is_set() and sources.source.closed
    assert calls == 1
    release.set()
    await factory.close()


@pytest.mark.asyncio
async def test_expired_completed_load_is_replaced_without_blocking_event_loop() -> None:
    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    expired = asyncio.create_task(
        asyncio.sleep(0, result=ProjectionSnapshot(shared_state(1), 0, 0))
    )
    await expired
    factory._loads[("participant", EVENT_ID)] = expired
    sources.subscribed = True
    snapshot = await asyncio.wait_for(factory._load_once("participant", EVENT_ID), 1)
    assert snapshot.valid_until > monotonic()
    assert projection.calls == 1
    await factory.close()
