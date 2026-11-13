"""模擬接続で隔離・共有復旧を確認する。600人の実負荷証明ではない。"""

import asyncio
from dataclasses import replace
from time import monotonic
from typing import Literal, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import WebSocket
from sqlalchemy import Engine
from test_common_state_cache import EVENT_ID, Projection, Source, Sources, shared_state
from test_personal_results import batch, common, principal
from test_realtime_state_auth import Factory, Socket, verified

from quiz_app.audience.contracts import ParticipantCommonStateResponse, PersonalResultResponse
from quiz_app.audience.result_delivery import PersonalResultService
from quiz_app.models.enums import EventState
from quiz_app.realtime import router
from quiz_app.realtime.contracts import InvalidationCause, RealtimeAudience
from quiz_app.realtime.notification_dispatcher import NotificationDispatcher
from quiz_app.realtime.redis_adapter import AsyncRealtimePublisher, RealtimeUnavailableError
from quiz_app.realtime.router import _serve_states
from quiz_app.realtime.state_stream import StateStreamFactory


async def until(predicate: object, deadline_seconds: float = 2) -> None:
    from collections.abc import Callable

    check = cast(Callable[[], bool], predicate)
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    handle: asyncio.TimerHandle | None = None

    def observe() -> None:
        nonlocal handle
        if check():
            ready.set()
        else:
            handle = loop.call_later(0.001, observe)

    observe()
    try:
        async with asyncio.timeout(deadline_seconds):
            await ready.wait()
    finally:
        if handle is not None:
            handle.cancel()


@pytest.mark.asyncio
async def test_600_mock_connections_isolate_two_slow_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "SEND_TIMEOUT_SECONDS", 0.05)
    sockets = [Socket(slow_send=index < 2) for index in range(600)]
    factories = [Factory() for _ in sockets]
    baseline = asyncio.all_tasks()
    tasks = [
        asyncio.create_task(
            _serve_states(
                cast(WebSocket, socket),
                "participant",
                EVENT_ID,
                cast(StateStreamFactory, factory),
                verified(),
                verified,
            )
        )
        for socket, factory in zip(sockets, factories, strict=True)
    ]
    try:
        await until(lambda: all(socket.state_received.is_set() for socket in sockets[2:]))
        await until(lambda: all(socket.closed == 1013 for socket in sockets[:2]))
        assert sum(socket.state_received.is_set() for socket in sockets) == 598
        assert sum(socket.closed == 1013 for socket in sockets) == 2
        assert all(socket.closed is None for socket in sockets[2:])
        await asyncio.gather(*tasks[:2])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(factory.subscription.closed for factory in factories)
    assert asyncio.all_tasks() <= baseline


class RecoverableSources(Sources):
    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def subscribe(self, audience: str, event_id: UUID) -> Source:
        if self.down:
            self.subscribed = True
            self.calls += 1
            raise RealtimeUnavailableError("fixture_redis_only")
        if self.source.closed:
            self.source = Source()
        return await super().subscribe(audience, event_id)


@pytest.mark.asyncio
async def test_redis_loss_keeps_all_subscribers_and_recovers_with_one_resubscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_stream.RECONCILIATION_SECONDS", 0.02)
    sources = RecoverableSources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscriptions = await asyncio.gather(
        *(factory.subscribe("participant", EVENT_ID) for _ in range(40))
    )
    await asyncio.gather(*(item.next_message() for item in subscriptions))
    try:
        sources.down = True
        sources.source.pending.put_nowait(None)
        updates = await asyncio.wait_for(
            asyncio.gather(*(item.next_message() for item in subscriptions)), 1
        )
        assert all(state.event.event_version >= 2 for state in updates)
        assert not subscriptions[0]._hub.failed
        assert sources.calls == 1
        sources.down = False
        await until(lambda: sources.calls == 2, deadline_seconds=2)
        assert subscriptions[0]._hub.source is not None
        assert all(item._hub is subscriptions[0]._hub for item in subscriptions)
    finally:
        await asyncio.gather(*(item.close() for item in subscriptions))
        await factory.close()


@pytest.mark.asyncio
async def test_initial_redis_outage_accepts_db_snapshot_and_has_bounded_shared_retry() -> None:
    sources = RecoverableSources()
    sources.down = True
    factory = StateStreamFactory(sources, audience_projection=Projection(sources))
    subscription = await factory.subscribe("participant", EVENT_ID)
    try:
        assert (await subscription.next_message()).event.event_version == 1
        await asyncio.sleep(0.05)
        assert sources.calls == 1
        assert not subscription._hub.failed
    finally:
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_db_failure_does_not_turn_freshness_requests_into_retry_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quiz_app.realtime.state_stream.RECONCILIATION_SECONDS", 0.01)
    sources = RecoverableSources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscription = await factory.subscribe("participant", EVENT_ID)
    await subscription.next_message()
    projection.fail = True
    waiting: list[asyncio.Task[object]] = []
    try:
        await until(lambda: subscription._hub.failures == 1)
        calls = projection.calls
        waiting = [asyncio.create_task(subscription.fresh_message()) for _ in range(100)]
        await asyncio.sleep(0.05)
        assert projection.calls == calls
        assert all(not task.done() for task in waiting)
        assert subscription._hub.retry_at - monotonic() > 4
        assert not subscription._hub.failed
    finally:
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_redis_degraded_active_event_without_current_round_reconciles_in_one_second() -> None:
    sources = Sources()
    factory = StateStreamFactory(sources, audience_projection=Projection(sources))
    subscription = await factory.subscribe("participant", EVENT_ID)
    try:
        hub = subscription._hub
        state = shared_state(1)
        state = state.model_copy(
            update={"event": state.event.model_copy(update={"event_state": EventState.IN_PROGRESS})}
        )
        hub.snapshot = replace(hub.snapshot, state=state)
        hub.source = None
        assert factory._refresh_at(hub) == hub.snapshot.sampled_from + 1
    finally:
        await subscription.close()
        await factory.close()


class BlockingPublisher:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.calls: list[InvalidationCause] = []

    async def notify(
        self, _event_id: UUID, _audiences: tuple[RealtimeAudience, ...], *, cause: InvalidationCause
    ) -> bool:
        self.calls.append(cause)
        if cause == "participant_answer":
            self.entered.set()
            await asyncio.Future()
        return True


@pytest.mark.asyncio
async def test_dispatcher_enqueue_returns_without_redis_and_urgent_overtakes_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "quiz_app.realtime.notification_dispatcher.invalidate_local_common_state", lambda *_: None
    )
    sources = Sources()
    factory = StateStreamFactory(sources, audience_projection=Projection(sources))
    subscription = await factory.subscribe("participant", EVENT_ID)
    publisher = BlockingPublisher()
    dispatcher = NotificationDispatcher(
        cast(AsyncRealtimePublisher, publisher), factory, timeout=0.5
    )
    await dispatcher.start()
    try:
        assert dispatcher.notify(EVENT_ID, ("operator",), cause="participant_answer")
        await asyncio.wait_for(publisher.entered.wait(), 1)
        assert dispatcher.notify(EVENT_ID, ("participant",), cause="operator_mutation")
        await until(lambda: "operator_mutation" in publisher.calls, deadline_seconds=0.1)
        assert not dispatcher._tasks[False].done()
    finally:
        await dispatcher.close()
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_personal_result_ready_wakes_socket_without_common_state_or_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 60)
    service = PersonalResultService(cast(Engine, object()), loader=lambda _engine, key: batch(key))
    service.set_deferred(EVENT_ID, "fixture", True)
    service.start()
    state = common()
    service.observe(state)
    factory, socket = Factory(), Socket()
    factory.subscription.snapshot_for_send = lambda: (state, monotonic() + 10)
    auth = replace(verified(), principal=principal())
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            auth,
            lambda: auth,
            personal_results=service,
        )
    )
    try:
        await asyncio.wait_for(socket.state_received.wait(), 1)
        assert not any(message["type"] == "participant.result" for message in socket.sent)
        service.set_deferred(EVENT_ID, "fixture", False)
        await until(lambda: any(message["type"] == "participant.result" for message in socket.sent))
        personal = next(
            message for message in socket.sent if message["type"] == "participant.result"
        )
        assert personal["result"]["result"]["selected_option_id"] == str(UUID(int=3))
        outcome = personal["result"]["result"]["outcome"]
        assert outcome["base_points"] == 400
        assert outcome["speed_points"] == 100
        assert outcome["elapsed_ms"] == 2450
        common_message = next(
            message for message in socket.sent if message["type"] == "participant.state"
        )
        assert set(common_message["state"]["latest_result"]["result"]) == {
            "publication",
            "correct_option_id",
            "distribution",
        }
        assert [message["type"] for message in socket.sent] == [
            "participant.state",
            "participant.result",
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "hidden", "principal"])
async def test_delivered_personal_revision_skips_dto_until_revision_or_principal_changes(
    monkeypatch: pytest.MonkeyPatch, change: Literal["revision", "hidden", "principal"]
) -> None:
    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(router, "PARTICIPANT_REVALIDATION_SECONDS", 0.02)
    service = PersonalResultService(cast(Engine, object()), loader=lambda _engine, key: batch(key))
    service.start()
    state = common()
    service.observe(state)
    await until(lambda: service.delivery(state).status == "ready")
    result_for = Mock(wraps=service.result_for)
    monkeypatch.setattr(service, "result_for", result_for)
    factory, socket = Factory(), Socket()
    messages: asyncio.Queue[ParticipantCommonStateResponse] = asyncio.Queue()
    messages.put_nowait(state)
    factory.subscription.next_message = messages.get
    factory.subscription.snapshot_for_send = lambda: (state, monotonic() + 10)
    auth = replace(verified(), principal=principal())
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            auth,
            lambda: replace(auth, verified_from=monotonic()),
            personal_results=service,
        )
    )
    try:
        await until(lambda: any(message["type"] == "participant.result" for message in socket.sent))
        messages.put_nowait(state)
        service.invalidate(EVENT_ID)
        service.observe(state)
        await until(lambda: sum(message["type"] == "heartbeat" for message in socket.sent) >= 2)
        assert sum(message["type"] == "participant.state" for message in socket.sent) == 2
        assert result_for.call_count == 1

        if change == "principal":
            auth = replace(auth, principal=replace(principal(UUID(int=999)), public_label="XYZ"))
        else:
            state = common(2, visible=change != "hidden")
            service.invalidate(EVENT_ID)
            service.observe(state)
            messages.put_nowait(state)
        await until(
            lambda: sum(message["type"] == "participant.result" for message in socket.sent) == 2
        )
        calls_after_change = result_for.call_count
        assert calls_after_change >= 2
        if change != "hidden":
            assert calls_after_change == 2
        heartbeat_count = sum(message["type"] == "heartbeat" for message in socket.sent)
        await until(
            lambda: (
                sum(message["type"] == "heartbeat" for message in socket.sent)
                >= heartbeat_count + 2
            )
        )
        assert result_for.call_count == calls_after_change
        response = PersonalResultResponse.model_validate(
            next(
                message
                for message in reversed(socket.sent)
                if message["type"] == "participant.result"
            )["result"]
        )
        assert response.result is not None
        if change == "principal":
            assert any(message["type"] == "session.changed" for message in socket.sent)
            assert response.result.selected_option_id is None
            assert response.result.outcome is not None
            assert response.result.outcome.status == "Unanswered"
        else:
            assert response.delivery.revision is not None
            assert response.delivery.revision.event_version == 2
            if change == "hidden":
                assert response.result.ranking is None
                assert response.result.outcome is not None
                assert response.result.outcome.status == "Scored"
                assert response.result.outcome.speed_rank is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_personal_payload_invalidated_during_serialization_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_realtime_state_auth import Subscription

    from quiz_app.realtime.contracts import ParticipantResultMessage

    state = common()
    hidden = common(2, visible=False).model_copy(update={"latest_result": None})
    service = PersonalResultService(cast(Engine, object()), loader=lambda _engine, key: batch(key))
    service.start()
    service.observe(state)
    changed = asyncio.Event()

    class ChangingSubscription(Subscription):
        current = state

        def snapshot_for_send(self) -> tuple[object, float]:
            return self.current, monotonic() + 10

    subscription = ChangingSubscription()
    original = ParticipantResultMessage.model_dump_json

    def serialize(message: ParticipantResultMessage) -> str:
        payload = original(message)
        subscription.current = hidden
        service.invalidate(EVENT_ID)
        service.observe(hidden)
        changed.set()
        return payload

    monkeypatch.setattr(ParticipantResultMessage, "model_dump_json", serialize)
    factory, socket = Factory(), Socket()
    factory.subscription = subscription
    auth = replace(verified(), principal=principal())
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            auth,
            lambda: auth,
            personal_results=service,
        )
    )
    try:
        await asyncio.wait_for(changed.wait(), 1)
        await asyncio.sleep(0)
        assert all(message["type"] != "participant.result" for message in socket.sent)
        assert socket.closed is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_control_during_common_build_discards_older_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event as ThreadEvent

    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscription = await factory.subscribe("participant", EVENT_ID)
    await subscription.next_message()
    entered, release = ThreadEvent(), ThreadEvent()
    load = projection.get_common_snapshot

    def blocked(event_id: UUID, *, normal: bool = False) -> object:
        result = load(event_id)
        if projection.calls == 2:
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(projection, "get_common_snapshot", blocked)
    try:
        factory.invalidate(EVENT_ID, ("participant",), "participant_answer")
        assert await asyncio.to_thread(entered.wait, 1)
        factory.invalidate(EVENT_ID, ("participant",), "operator_mutation")
        release.set()
        state = await asyncio.wait_for(subscription.next_message(), 1)
        assert state.event.event_version == 3
        assert projection.calls == 3
        assert not subscription._hub.urgent
    finally:
        release.set()
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_join_notifications_have_one_fixed_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "quiz_app.realtime.notification_dispatcher.invalidate_local_common_state", lambda *_: None
    )
    sources = Sources()
    factory = StateStreamFactory(sources, audience_projection=Projection(sources))
    publisher = BlockingPublisher()
    dispatcher = NotificationDispatcher(
        cast(AsyncRealtimePublisher, publisher), factory, timeout=0.5
    )
    await dispatcher.start()
    started = monotonic()
    try:
        assert dispatcher.notify(EVENT_ID, ("operator",), cause="participant_join")
        await asyncio.sleep(0.1)
        for _ in range(600):
            assert dispatcher.notify(EVENT_ID, ("operator",), cause="participant_join")
        await asyncio.sleep(0)
        assert len(dispatcher._pending[False]) == 1
        assert not publisher.calls
        await until(lambda: len(publisher.calls) == 1, deadline_seconds=1)
        assert monotonic() - started < 0.6
        assert publisher.calls == ["participant_join"]
    finally:
        await dispatcher.close()
        await factory.close()


@pytest.mark.asyncio
async def test_successful_db_read_slower_than_one_second_enters_shared_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quiz_app.realtime.state_cache import ProjectionSnapshot

    now = [monotonic()]
    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection, clock=lambda: now[0])
    subscription = await factory.subscribe("participant", EVENT_ID)
    await subscription.next_message()
    hub = subscription._hub
    hub.source = None
    state = shared_state(2)
    state = state.model_copy(
        update={"event": state.event.model_copy(update={"event_state": EventState.IN_PROGRESS})}
    )

    def slow_success(_event_id: UUID) -> object:
        now[0] += 1.5
        return ProjectionSnapshot(state, now[0], now[0] + 5)

    monkeypatch.setattr(projection, "get_common_snapshot", slow_success)
    try:
        factory.invalidate(EVENT_ID, ("participant",), "operator_mutation")
        assert (await asyncio.wait_for(subscription.next_message(), 1)).event.event_version == 2
        await until(lambda: hub.failures == 1)
        assert hub.retry_at == now[0] + 5
        assert not hub.failed
    finally:
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_answer_batch_does_not_wait_again_behind_join_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "quiz_app.realtime.notification_dispatcher.invalidate_local_common_state", lambda *_: None
    )
    sources = Sources()
    factory = StateStreamFactory(sources, audience_projection=Projection(sources))
    publisher = BlockingPublisher()
    dispatcher = NotificationDispatcher(
        cast(AsyncRealtimePublisher, publisher), factory, timeout=0.5
    )
    await dispatcher.start()
    try:
        dispatcher.notify(EVENT_ID, ("operator",), cause="participant_join")
        await asyncio.sleep(0)
        dispatcher.notify(EVENT_ID, ("operator",), cause="participant_answer")
        await asyncio.wait_for(publisher.entered.wait(), 0.1)
        assert publisher.calls == ["participant_answer"]
    finally:
        await dispatcher.close()
        await factory.close()


@pytest.mark.asyncio
async def test_full_normal_read_budget_defers_display_but_control_bypasses_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quiz_app.realtime.projection_budget import ProjectionReadDeferred

    sources = Sources()
    projection = Projection(sources)
    factory = StateStreamFactory(sources, audience_projection=projection)
    subscription = await factory.subscribe("participant", EVENT_ID)
    await subscription.next_message()
    load = projection.get_common_snapshot
    attempts: list[bool] = []

    def busy(event_id: UUID, *, normal: bool = False) -> object:
        attempts.append(normal)
        if normal:
            raise ProjectionReadDeferred("fixture_display_budget_full")
        return load(event_id)

    monkeypatch.setattr(projection, "get_common_snapshot", busy)
    try:
        factory.invalidate(EVENT_ID, ("participant",), "participant_answer")
        await until(lambda: subscription._hub.normal_at is not None and attempts == [True])
        assert subscription._hub.failures == 0
        assert subscription._hub.retry_at == 0
        factory.invalidate(EVENT_ID, ("participant",), "operator_mutation")
        state = await asyncio.wait_for(subscription.next_message(), 0.2)
        assert state.event.event_version == 2
        assert attempts == [True, False]
    finally:
        await subscription.close()
        await factory.close()


@pytest.mark.asyncio
async def test_expired_ready_snapshot_heartbeat_withdraws_http_recovery_without_closing_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_realtime_state_auth import Subscription

    from quiz_app.realtime.state_stream import StateSnapshotRefreshRequired

    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 0.01)
    state = common()
    service = PersonalResultService(cast(Engine, object()), loader=lambda _engine, key: batch(key))
    service.start()
    service.observe(state)
    await until(lambda: service.delivery(state).status == "ready")

    class ExpiringSubscription(Subscription):
        is_fresh = True

        def snapshot_for_send(self) -> tuple[object, float]:
            if not self.is_fresh:
                raise StateSnapshotRefreshRequired("fixture_snapshot_expired")
            return state, monotonic() + 10

    subscription = ExpiringSubscription()

    class ExpiringSocket(Socket):
        async def send_json(self, message: dict[str, object]) -> None:
            await super().send_json(message)
            if message["type"] == "participant.state":
                subscription.is_fresh = False

    factory, socket = Factory(), ExpiringSocket()
    factory.subscription = subscription
    auth = replace(verified(), principal=principal())
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            auth,
            lambda: auth,
            personal_results=service,
        )
    )
    try:
        await asyncio.wait_for(socket.control_received.wait(), 1)
        common_message = next(
            message for message in socket.sent if message["type"] == "participant.state"
        )
        heartbeat = next(message for message in socket.sent if message["type"] == "heartbeat")
        assert common_message["state"]["personal_result_delivery"]["status"] == "ready"
        assert heartbeat["personal_result_delivery"] == {"status": "unavailable", "revision": None}
        assert not any(message["type"] == "participant.result" for message in socket.sent)
        assert socket.closed is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
