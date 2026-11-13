"""長寿命WSの認証期限、属性変更、遅いsendと回収を確認する。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from typing import Literal, cast
from uuid import UUID

import pytest
from fastapi import WebSocket
from pydantic import TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from quiz_app.audience.auth_service import ParticipantPrincipal, VerifiedParticipantAuthentication
from quiz_app.audience.contracts import ParticipantCommonStateResponse
from quiz_app.audience.errors import ParticipantSessionInvalidError
from quiz_app.config import Settings
from quiz_app.models.enums import AdminRole
from quiz_app.models.identity import AdminAccount, AdminSession
from quiz_app.observability.realtime_failures import (
    RealtimeFailureBoundary,
    RealtimeFailureRecord,
    realtime_failure_record,
)
from quiz_app.operator.auth_service import OperatorAuthService
from quiz_app.operator.errors import OperatorSessionInvalidError
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime import router
from quiz_app.realtime.contracts import (
    HeartbeatMessage,
    ParticipantStateMessage,
    RealtimeAudience,
    RealtimeServerMessage,
)
from quiz_app.realtime.redis_adapter import RealtimeUnavailableError
from quiz_app.realtime.router import _serve_states, authentication_deadlines
from quiz_app.realtime.state_stream import StateSnapshotRefreshRequired, StateStreamFactory

EVENT_ID = UUID(int=1)
PRINCIPAL = ParticipantPrincipal(
    participant_id=UUID(int=2),
    session_id=UUID(int=3),
    event_id=EVENT_ID,
    public_label="AAA",
    is_ranking_eligible=True,
)


def verified(lifetime: float = 10.0) -> VerifiedParticipantAuthentication:
    now = datetime.now(UTC)
    return VerifiedParticipantAuthentication(
        principal=PRINCIPAL,
        database_time=now,
        expires_at=now + timedelta(seconds=lifetime),
        verified_from=monotonic(),
    )


def common_state() -> ParticipantCommonStateResponse:
    return ParticipantCommonStateResponse(
        award_ceremony=None,
        projected_at=datetime.now(UTC),
        event={
            "timing_basis": "server_received_v1",
            "arrival_grace_ms": 0,
            "clock_tolerance_ms": 0,
            "client_ahead_tolerance_ms": 0,
            "event_id": EVENT_ID,
            "event_title": "共通",
            "event_state": "LobbyOpen",
            "event_version": 1,
            "start_delay_seconds": 3,
        },
        current_round=None,
        latest_result=None,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        ),
        latest_ranking_snapshot_version=None,
    )


class Subscription:
    def __init__(self) -> None:
        self.initial = True
        self.closed = False

    async def next_message(self) -> ParticipantCommonStateResponse:
        if self.initial:
            self.initial = False
            return common_state()
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True

    def snapshot_for_send(self) -> tuple[ParticipantCommonStateResponse, float]:
        return common_state(), monotonic() + 1


class Factory:
    def __init__(self) -> None:
        self.subscription = Subscription()

    async def subscribe(self, _: str, __: UUID) -> Subscription:
        return self.subscription


class Socket:
    def __init__(self, *, slow_send: bool = False) -> None:
        self.scope: dict[str, object] = {}
        self.sent: list[dict[str, object]] = []
        self.closed: int | None = None
        self.control_received = asyncio.Event()
        self.state_received = asyncio.Event()
        self.slow_send = slow_send

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict[str, object]:
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def send_json(self, message: dict[str, object]) -> None:
        if self.slow_send and message["type"] == "participant.state":
            await asyncio.Future()
        self.sent.append(message)
        if message["type"] in {"session.changed", "heartbeat"}:
            self.control_received.set()
        if message["type"] in {"participant.state", "operator.state"}:
            self.state_received.set()

    async def send_text(self, message: str) -> None:
        parsed = TypeAdapter(RealtimeServerMessage).validate_json(message)
        await self.send_json(parsed.model_dump(mode="json"))

    async def close(self, *, code: int) -> None:
        self.closed = code


def test_revalidation_is_bounded_by_database_expiration_and_started_clock() -> None:
    context = replace(verified(30), verified_from=100.0)
    assert authentication_deadlines(context, 60.0) == (130.0, 130.0)
    assert authentication_deadlines(context, 1.0) == (101.0, 130.0)


@pytest.mark.asyncio
async def test_revoked_session_closes_without_new_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "PARTICIPANT_REVALIDATION_SECONDS", 0.02)
    socket, factory = Socket(), Factory()

    def revoke() -> VerifiedParticipantAuthentication:
        raise ParticipantSessionInvalidError("fixture_revoked")

    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(),
            revoke,
        ),
        1,
    )
    assert socket.closed == 4401
    assert [message["type"] for message in socket.sent] == ["participant.state"]
    assert factory.subscription.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoked", "timeout", "expired"])
async def test_async_revalidation_keeps_deadlines_without_borrowing_operator_threads(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(router, "PARTICIPANT_REVALIDATION_SECONDS", 0.02)
    monkeypatch.setattr(router, "SEND_TIMEOUT_SECONDS", 0.04)
    socket, factory = Socket(), Factory()
    completed = asyncio.Event()
    calls = 0

    async def reject_threadpool(*_args: object) -> None:
        raise AssertionError("participant_auth_must_not_use_operator_threadpool")

    async def refresh() -> VerifiedParticipantAuthentication:
        nonlocal calls
        calls += 1
        try:
            if failure == "revoked":
                raise ParticipantSessionInvalidError("fixture_revoked")
            await asyncio.Future()
            raise AssertionError("unreachable")
        finally:
            completed.set()

    monkeypatch.setattr(router, "run_in_threadpool", reject_threadpool)
    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(0.04 if failure == "expired" else 10.0),
            async_verify=refresh,
        ),
        1,
    )
    assert calls == 1
    assert completed.is_set()
    assert socket.closed == (1013 if failure == "timeout" else 4401)
    assert [message["type"] for message in socket.sent] == (
        ["participant.state", "polling.required"] if failure == "timeout" else ["participant.state"]
    )
    assert factory.subscription.closed


@pytest.mark.asyncio
async def test_absolute_expiration_stops_payload_even_during_blocked_send() -> None:
    socket, factory = Socket(slow_send=True), Factory()
    baseline = asyncio.all_tasks()
    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(0.02),
            verified,
        ),
        1,
    )
    assert socket.closed == 4401
    assert socket.sent == []
    assert factory.subscription.closed
    assert asyncio.all_tasks() <= baseline


@pytest.mark.asyncio
async def test_valid_attribute_change_emits_session_control_and_increments_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "PARTICIPANT_REVALIDATION_SECONDS", 0.02)
    socket, factory = Socket(), Factory()

    def changed() -> VerifiedParticipantAuthentication:
        return replace(verified(), principal=replace(PRINCIPAL, public_label="BBB"))

    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(),
            changed,
        )
    )
    try:
        await asyncio.wait_for(socket.control_received.wait(), 1)
        first, control = socket.sent
        assert first["type"] == "participant.state"
        assert control["type"] == "session.changed"
        assert control["sequence"] == first["sequence"] + 1
        assert control["stream_id"] == first["stream_id"]
        assert "state" not in control
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert factory.subscription.closed


@pytest.mark.asyncio
async def test_heartbeat_does_not_get_personal_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 0.02)
    socket, factory = Socket(), Factory()
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(),
            verified,
        )
    )
    try:
        await asyncio.wait_for(socket.control_received.wait(), 1)
        first, heartbeat = socket.sent
        assert heartbeat["type"] == "heartbeat"
        assert heartbeat["stream_id"] == first["stream_id"]
        assert heartbeat["sequence"] == 2
        assert "state" not in heartbeat
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert factory.subscription.closed


@pytest.mark.asyncio
async def test_state_expiring_during_authentication_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "PARTICIPANT_REVALIDATION_SECONDS", 0.02)
    now = [100.0]

    class ExpiringSubscription(Subscription):
        async def next_message(self) -> ParticipantCommonStateResponse:
            now[0] = 100.02
            return common_state()

        def snapshot_for_send(self) -> tuple[ParticipantCommonStateResponse, float]:
            if now[0] >= 101.0:
                raise RealtimeUnavailableError("state_snapshot_expired")
            return common_state(), 101.0

    socket, factory = Socket(), Factory()
    factory.subscription = ExpiringSubscription()

    def slow_authentication() -> VerifiedParticipantAuthentication:
        now[0] = 101.1
        return replace(verified(), verified_from=101.1)

    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            replace(verified(), verified_from=100.0),
            slow_authentication,
            clock=lambda: now[0],
        ),
        1,
    )
    assert [message["type"] for message in socket.sent] == ["polling.required"]
    assert socket.closed == 1013
    assert factory.subscription.closed


@pytest.mark.parametrize("lifetime", [-1, 0, 1])
def test_operator_realtime_auth_is_one_read_only_query_with_absolute_expiry(
    monkeypatch: pytest.MonkeyPatch, lifetime: int
) -> None:
    database_time = datetime.now(UTC)
    fixture_digest = "fixture"
    account = AdminAccount(
        id=UUID(int=10), username="operator", password_hash=fixture_digest, role=AdminRole.OPERATOR
    )
    admin_session = AdminSession(
        id=UUID(int=11),
        account_id=account.id,
        token_hash=fixture_digest,
        csrf_hash="fixture-csrf",
        expires_at=database_time + timedelta(seconds=lifetime),
        last_action_at=database_time - timedelta(seconds=10),
    )
    statements: list[Select] = []

    class ReadSession:
        def __init__(self, _: object) -> None:
            pass

        def __enter__(self) -> "ReadSession":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def execute(self, statement: Select) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(one_or_none=lambda: (admin_session, account, database_time))

    monkeypatch.setattr("quiz_app.operator.auth_service.Session", ReadSession)
    service = OperatorAuthService(create_engine("sqlite://"), Settings(_env_file=None))
    if lifetime <= 0:
        with pytest.raises(OperatorSessionInvalidError):
            service.authenticate_realtime("fixture-token")
    else:
        context = service.authenticate_realtime("fixture-token")
        assert context.database_time == database_time
        assert context.expires_at == admin_session.expires_at
    assert admin_session.last_action_at == database_time - timedelta(seconds=10)
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "admin_sessions.revoked_at IS NULL" in sql
    assert "admin_sessions.expires_at > clock_timestamp()" in sql
    assert "admin_accounts.is_revoked IS false" in sql
    assert "FOR UPDATE" not in sql


@pytest.mark.asyncio
async def test_initial_snapshot_wait_keeps_heartbeat_and_cleans_pending_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 0.01)

    class WaitingFactory(Factory):
        def __init__(self) -> None:
            super().__init__()
            self.ready = asyncio.Event()

        async def subscribe(self, _: str, __: UUID) -> Subscription:
            await self.ready.wait()
            return self.subscription

    socket, factory = Socket(), WaitingFactory()
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(),
            verified,
        )
    )
    try:
        await asyncio.wait_for(socket.control_received.wait(), 1)
        assert [message["type"] for message in socket.sent] == ["heartbeat"]
        factory.ready.set()
        await asyncio.wait_for(socket.state_received.wait(), 1)
        assert socket.sent[-1]["type"] == "participant.state"
        assert socket.sent[-1]["sequence"] > socket.sent[0]["sequence"]
        assert socket.sent[-1]["stream_id"] == socket.sent[0]["stream_id"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert factory.subscription.closed


@pytest.mark.asyncio
async def test_absolute_expiry_cancels_initial_subscribe_without_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 0.005)

    class WaitingFactory(Factory):
        cancelled = False

        async def subscribe(self, _: str, __: UUID) -> Subscription:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    socket, factory = Socket(), WaitingFactory()
    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(0.02),
            verified,
        ),
        1,
    )
    assert socket.closed == 4401
    assert factory.cancelled
    assert socket.sent and all(message["type"] == "heartbeat" for message in socket.sent)


@pytest.mark.asyncio
async def test_expired_snapshot_wait_keeps_heartbeat_then_sends_fresh_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "HEARTBEAT_SECONDS", 0.01)

    class RefreshingSubscription(Subscription):
        def __init__(self) -> None:
            super().__init__()
            self.changed = asyncio.Event()
            self.ready = asyncio.Event()
            self.expired = False
            self.refreshes = 0

        async def next_message(self) -> ParticipantCommonStateResponse:
            if self.initial:
                self.initial = False
                return common_state()
            await self.changed.wait()
            self.changed.clear()
            self.expired = True
            return common_state()

        def snapshot_for_send(self) -> tuple[ParticipantCommonStateResponse, float]:
            if self.expired:
                raise StateSnapshotRefreshRequired("state_snapshot_expired")
            return common_state(), monotonic() + 1

        async def fresh_message(self) -> ParticipantCommonStateResponse:
            self.refreshes += 1
            await self.ready.wait()
            self.expired = False
            return common_state()

    socket, factory = Socket(), Factory()
    subscription = RefreshingSubscription()
    factory.subscription = subscription
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            verified(),
            verified,
        )
    )
    try:
        await asyncio.wait_for(socket.state_received.wait(), 1)
        socket.state_received.clear()
        socket.control_received.clear()
        subscription.changed.set()
        await asyncio.wait_for(socket.control_received.wait(), 1)
        assert subscription.refreshes == 1
        assert socket.sent[-1]["type"] == "heartbeat"
        assert not socket.state_received.is_set()
        assert socket.closed is None
        subscription.ready.set()
        await asyncio.wait_for(socket.state_received.wait(), 1)
        assert socket.sent[-1]["type"] == "participant.state"
        assert "polling.required" not in [message["type"] for message in socket.sent]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert subscription.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("crossing_message", ["state", "heartbeat"])
async def test_authentication_deadline_during_serialization_defers_unsent_payload(
    monkeypatch: pytest.MonkeyPatch, crossing_message: Literal["state", "heartbeat"]
) -> None:
    now = [159.99 if crossing_message == "state" else 159.99 - router.HEARTBEAT_SECONDS]
    initial = replace(verified(720), verified_from=100.0)
    checks: list[VerifiedParticipantAuthentication] = []
    crossed = False
    model = ParticipantStateMessage if crossing_message == "state" else HeartbeatMessage
    original_dump = model.model_dump_json

    def dump(message: ParticipantStateMessage | HeartbeatMessage) -> str:
        nonlocal crossed
        if not crossed:
            crossed = True
            now[0] = 160.01
        return original_dump(message)

    monkeypatch.setattr(model, "model_dump_json", dump)

    class CurrentSubscription(Subscription):
        async def next_message(self) -> ParticipantCommonStateResponse:
            if self.initial and crossing_message == "heartbeat":
                now[0] = 159.99
            return await super().next_message()

        def snapshot_for_send(self) -> tuple[ParticipantCommonStateResponse, float]:
            return common_state(), now[0] + 1

    class CurrentSocket(Socket):
        async def send_json(self, message: dict[str, object]) -> None:
            assert now[0] < (checks[-1].verified_from + 60 if checks else 160)
            await super().send_json(message)

    def verify() -> VerifiedParticipantAuthentication:
        context = replace(
            initial,
            verified_from=now[0],
            database_time=initial.database_time + timedelta(seconds=now[0] - 100),
        )
        checks.append(context)
        return context

    socket, factory = CurrentSocket(), Factory()
    factory.subscription = CurrentSubscription()
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            initial,
            verify,
            clock=lambda: now[0],
        )
    )
    try:
        ready = socket.state_received if crossing_message == "state" else socket.control_received
        await asyncio.wait_for(ready.wait(), 1)
        assert len(checks) == 1
        assert authentication_deadlines(checks[0], 60) == pytest.approx((220.01, 820.0))
        assert [message["type"] for message in socket.sent] == (
            ["participant.state"]
            if crossing_message == "state"
            else ["heartbeat", "participant.state"]
        )
        assert [message["sequence"] for message in socket.sent] == list(
            range(1, len(socket.sent) + 1)
        )
        assert len({message["stream_id"] for message in socket.sent}) == 1
        assert socket.closed is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert factory.subscription.closed
    assert asyncio.all_tasks() <= baseline


@pytest.mark.asyncio
async def test_absolute_expiration_during_json_serialization_sends_no_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    original_dump = ParticipantStateMessage.model_dump_json

    def dump(message: ParticipantStateMessage) -> str:
        now[0] = 100.02
        return original_dump(message)

    monkeypatch.setattr(ParticipantStateMessage, "model_dump_json", dump)

    def verify() -> VerifiedParticipantAuthentication:
        raise AssertionError("absolute expiration must close without reauthentication")

    socket, factory = Socket(), Factory()
    baseline = asyncio.all_tasks()
    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            replace(verified(0.01), verified_from=100.0),
            verify,
            clock=lambda: now[0],
        ),
        1,
    )
    assert socket.sent == []
    assert socket.closed == 4401
    assert factory.subscription.closed
    assert asyncio.all_tasks() <= baseline


@pytest.mark.asyncio
async def test_state_expiration_during_json_serialization_refreshes_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    original_dump = ParticipantStateMessage.model_dump_json
    crossed = False

    def dump(message: ParticipantStateMessage) -> str:
        nonlocal crossed
        if not crossed:
            crossed = True
            now[0] = 100.02
        return original_dump(message)

    monkeypatch.setattr(ParticipantStateMessage, "model_dump_json", dump)

    class RefreshingSubscription(Subscription):
        current_until = 100.01
        refreshes = 0

        def snapshot_for_send(self) -> tuple[ParticipantCommonStateResponse, float]:
            return common_state(), self.current_until

        async def fresh_message(self) -> ParticipantCommonStateResponse:
            self.refreshes += 1
            self.current_until = now[0] + 1
            return common_state()

    subscription = RefreshingSubscription()

    class FreshSocket(Socket):
        async def send_json(self, message: dict[str, object]) -> None:
            assert now[0] < subscription.current_until
            await super().send_json(message)

    socket, factory = FreshSocket(), Factory()
    factory.subscription = subscription
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            replace(verified(720), verified_from=100.0),
            verified,
            clock=lambda: now[0],
        )
    )
    try:
        await asyncio.wait_for(socket.state_received.wait(), 1)
        assert subscription.refreshes == 1
        assert [message["type"] for message in socket.sent] == ["participant.state"]
        assert socket.sent[0]["sequence"] == 1
        assert socket.closed is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert subscription.closed
    assert asyncio.all_tasks() <= baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["state_send", "authentication_refresh"])
async def test_timeout_is_logged_safely_and_started_send_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: Literal["state_send", "authentication_refresh"],
) -> None:
    sensitive_fixture = "fixture-private-token-and-SQL-SELECT-body"
    records: list[RealtimeFailureRecord] = []
    state_attempts = 0

    def log_failure(
        error: BaseException,
        boundary: RealtimeFailureBoundary,
        *,
        audience: RealtimeAudience | None = None,
    ) -> None:
        records.append(realtime_failure_record(error, boundary, audience))

    monkeypatch.setattr(router, "log_realtime_failure", log_failure)

    class TimedOutSocket(Socket):
        async def send_json(self, message: dict[str, object]) -> None:
            nonlocal state_attempts
            if message["type"] == "participant.state":
                state_attempts += 1
                if failure_boundary == "state_send":
                    raise TimeoutError(sensitive_fixture) from ValueError(sensitive_fixture)
            await super().send_json(message)

    def verify() -> VerifiedParticipantAuthentication:
        raise TimeoutError(sensitive_fixture) from ValueError(sensitive_fixture)

    initial = verified()
    if failure_boundary == "authentication_refresh":
        initial = replace(initial, verified_from=monotonic() - 61)
        initial = replace(initial, expires_at=initial.database_time + timedelta(seconds=720))
    socket, factory = TimedOutSocket(), Factory()
    baseline = asyncio.all_tasks()
    await asyncio.wait_for(
        _serve_states(
            cast(WebSocket, socket),
            "participant",
            EVENT_ID,
            cast(StateStreamFactory, factory),
            initial,
            verify,
        ),
        1,
    )
    assert state_attempts == (1 if failure_boundary == "state_send" else 0)
    assert [message["type"] for message in socket.sent] == ["polling.required"]
    assert socket.closed == 1013
    assert factory.subscription.closed
    assert len(records) == 1
    record = records[0]
    assert record.boundary == failure_boundary
    assert record.failure_kind == "timeout"
    assert record.audience == "participant"
    assert record.stack_state == "available"
    assert any(frame.source_module == "quiz_app.realtime.router" for frame in record.stack)
    serialized = record.model_dump_json()
    assert sensitive_fixture not in serialized
    assert "ValueError" not in serialized
    assert asyncio.all_tasks() <= baseline
