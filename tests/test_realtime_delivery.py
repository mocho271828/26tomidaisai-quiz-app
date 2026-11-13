import asyncio
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import cast
from uuid import UUID

import logfire
import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import SQLAlchemyError
from starlette.websockets import WebSocketDisconnect

from quiz_app.audience.async_service import get_audience_async_service
from quiz_app.audience.auth_service import (
    ParticipantPrincipal,
    VerifiedParticipantAuthentication,
)
from quiz_app.audience.contracts import ParticipantCommonStateResponse
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.config import (
    OPERATOR_SESSION_COOKIE_NAME,
    PARTICIPANT_SESSION_COOKIE_NAME,
    Settings,
    get_settings,
)
from quiz_app.main import app
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import (
    OperatorPrincipal,
    VerifiedOperatorAuthentication,
    get_operator_auth_service,
)
from quiz_app.operator.control_contracts import (
    OperatorMutationRequest,
    OperatorMutationResponse,
    OperatorStateResponse,
)
from quiz_app.operator.control_router import begin_preparation
from quiz_app.operator.errors import OperatorSessionInvalidError
from quiz_app.operator.mutations import OperatorMutationService
from quiz_app.quiz_configuration import ResultVisibility, get_quiz_settings
from quiz_app.realtime.contracts import (
    PollingRequiredMessage,
    RealtimeAudience,
    RealtimeServerMessage,
    StateInvalidatedMessage,
)
from quiz_app.realtime.redis_adapter import (
    ALL_AUDIENCES,
    InvalidationCause,
    RealtimePublisher,
    RealtimeSubscription,
    RealtimeUnavailableError,
    RedisHealthProbe,
    RedisRealtimeSubscription,
    RedisRealtimeSubscriptionFactory,
    audiences_for_invalidation,
    realtime_channel,
)
from quiz_app.realtime.router import (
    AUTH_REQUIRED_CLOSE_CODE,
    ORIGIN_REJECTED_CLOSE_CODE,
    REALTIME_UNAVAILABLE_CLOSE_CODE,
    UNSUPPORTED_CLIENT_MESSAGE_CLOSE_CODE,
    _serve_states,
    is_allowed_origin,
)
from quiz_app.realtime.state_stream import get_state_stream_factory
from quiz_app.single_event import (
    SingleEventConfigurationError,
    get_single_event_service,
)

EVENT_ID = UUID("80000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("80000000-0000-4000-8000-000000000002")
ACCOUNT_ID = UUID("80000000-0000-4000-8000-000000000003")
SESSION_ID = UUID("80000000-0000-4000-8000-000000000004")
IDEMPOTENCY_KEY = UUID("80000000-0000-4000-8000-000000000005")
PARTICIPANT_SESSION_FIXTURE = "valid-participant-session"
OPERATOR_SESSION_FIXTURE = "valid-operator-session"


class RecordingRedisClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    def ping(self) -> bool:
        return True


class FailingRedisClient:
    def __init__(self, calls: list[str] | None = None) -> None:
        self._calls = calls

    def publish(self, _: str, __: str) -> int:
        if self._calls is not None:
            self._calls.append("publish")
        raise RedisConnectionError("redis unavailable")

    def ping(self) -> bool:
        raise RedisConnectionError("redis unavailable")


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


def operator_state() -> OperatorStateResponse:
    return OperatorStateResponse(
        award_ceremony_phase=None,
        projected_at=datetime.now(UTC),
        event_id=EVENT_ID,
        event_title="運営",
        event_state="LobbyOpen",
        event_version=1,
        start_delay_seconds=3,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        ),
        visibility_controls=get_quiz_settings().visibility,
        participant_count=0,
        available_questions=[],
        used_question_ids=[],
        current_round=None,
        latest_ranking_snapshot_version=None,
    )


class StubParticipantRealtimeAuthService:
    def authenticate_realtime(self, session_token: str) -> VerifiedParticipantAuthentication:
        now = datetime.now(UTC)
        return VerifiedParticipantAuthentication(
            principal=self.authenticate(session_token),
            database_time=now,
            expires_at=now + timedelta(hours=1),
            verified_from=monotonic(),
        )

    def authenticate(self, session_token: str) -> ParticipantPrincipal:
        if session_token != PARTICIPANT_SESSION_FIXTURE:
            raise ParticipantSessionInvalidError("invalid")
        return ParticipantPrincipal(
            participant_id=UUID("80000000-0000-4000-8000-000000000006"),
            session_id=UUID("80000000-0000-4000-8000-000000000007"),
            event_id=EVENT_ID,
            public_label="AAA",
            is_ranking_eligible=True,
        )


class StubParticipantAsyncRealtimeService:
    async def authenticate_realtime(self, session_token: str) -> VerifiedParticipantAuthentication:
        return StubParticipantRealtimeAuthService().authenticate_realtime(session_token)


class StubOperatorRealtimeAuthService:
    def authenticate_realtime(self, session_token: str) -> VerifiedOperatorAuthentication:
        now = datetime.now(UTC)
        return VerifiedOperatorAuthentication(
            principal=self.authenticate(session_token),
            database_time=now,
            expires_at=now + timedelta(hours=1),
            verified_from=monotonic(),
        )

    def authenticate(self, session_token: str) -> OperatorPrincipal:
        if session_token != OPERATOR_SESSION_FIXTURE:
            raise OperatorSessionInvalidError("invalid")
        return OperatorPrincipal(
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            username="demo-operator",
            role=AdminRole.OPERATOR,
            csrf_hash="stored",
        )


class StubSingleEventService:
    def get_event_id(self) -> UUID:
        return EVENT_ID

    def require_round(self, _: UUID) -> UUID:
        return EVENT_ID


class UnavailableSubscriptionFactory:
    async def subscribe(
        self,
        _: RealtimeAudience,
        __: UUID,
    ) -> RealtimeSubscription:
        raise RealtimeUnavailableError("redis unavailable")


class StubSubscription:
    def __init__(self, message: StateInvalidatedMessage) -> None:
        self._messages: asyncio.Queue[ParticipantCommonStateResponse | OperatorStateResponse] = (
            asyncio.Queue()
        )
        self._messages.put_nowait(
            common_state() if message.audience == "participant" else operator_state()
        )
        self.is_closed = False
        self.state = common_state() if message.audience == "participant" else operator_state()

    async def next_message(self) -> ParticipantCommonStateResponse | OperatorStateResponse:
        return await self._messages.get()

    async def close(self) -> None:
        self.is_closed = True

    def snapshot_for_send(
        self,
    ) -> tuple[ParticipantCommonStateResponse | OperatorStateResponse, float]:
        return self.state, monotonic() + 1


class StubSubscriptionFactory:
    def __init__(self, subscription: StubSubscription) -> None:
        self.subscription = subscription
        self.audience: RealtimeAudience | None = None
        self.event_id: UUID | None = None

    async def subscribe(
        self,
        audience: RealtimeAudience,
        event_id: UUID,
    ) -> RealtimeSubscription:
        self.audience = audience
        self.event_id = event_id
        return self.subscription


class BlockingSubscription:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.is_cancelled = False
        self.is_closed = False
        self._release = asyncio.Event()
        self._initial = True

    async def next_message(self) -> ParticipantCommonStateResponse:
        if self._initial:
            self._initial = False
            return common_state()
        self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.is_cancelled = True
            raise
        return common_state()

    async def close(self) -> None:
        self.is_closed = True

    def snapshot_for_send(self) -> tuple[ParticipantCommonStateResponse, float]:
        return common_state(), monotonic() + 1


class BlockingSubscriptionFactory:
    def __init__(self, subscription: BlockingSubscription) -> None:
        self._subscription = subscription

    async def subscribe(
        self,
        _: RealtimeAudience,
        __: UUID,
    ) -> RealtimeSubscription:
        return self._subscription


class BlockingWebSocket:
    def __init__(self) -> None:
        self.receive_started = asyncio.Event()
        self.is_receive_cancelled = False
        self.sent_messages: list[object] = []
        self._release = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def receive(self) -> dict[str, object]:
        self.receive_started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.is_receive_cancelled = True
            raise
        return {"type": "websocket.disconnect"}

    async def send_json(self, message: object) -> None:
        self.sent_messages.append(message)

    async def send_text(self, message: str) -> None:
        parsed = TypeAdapter(RealtimeServerMessage).validate_json(message)
        await self.send_json(parsed.model_dump(mode="json"))

    async def close(self, *, code: int) -> None:
        del code


@pytest.fixture
def realtime_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_audience_async_service] = StubParticipantAsyncRealtimeService
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    app.dependency_overrides[get_single_event_service] = StubSingleEventService
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_invalidation_payload_contains_only_routing_fields() -> None:
    client = RecordingRedisClient()
    publisher = RealtimePublisher(client)

    assert publisher.notify(EVENT_ID, ALL_AUDIENCES) is True
    assert [channel for channel, _ in client.published] == [
        realtime_channel("participant", EVENT_ID),
        realtime_channel("operator", EVENT_ID),
    ]
    for _, raw_message in client.published:
        message = StateInvalidatedMessage.model_validate_json(raw_message)
        assert set(message.model_dump(mode="json")) == {"type", "audience", "event_id", "cause"}

    with pytest.raises(ValidationError):
        StateInvalidatedMessage.model_validate(
            {
                "type": "state.invalidated",
                "audience": "participant",
                "event_id": EVENT_ID,
                "correct_option_id": UUID(int=99),
            }
        )
    assert set(StateInvalidatedMessage.model_json_schema()["required"]) == {
        "type",
        "audience",
        "event_id",
    }
    assert set(PollingRequiredMessage.model_json_schema()["required"]) == {
        "type",
        "reason",
        "poll_after_ms",
    }
    with pytest.raises(ValidationError):
        PollingRequiredMessage.model_validate({"type": "polling.required"})


def test_redis_failure_is_degraded_and_never_escapes_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logfire, "exception", lambda *_, **__: None)
    failing_client = FailingRedisClient()

    assert RealtimePublisher(failing_client).notify(EVENT_ID, ALL_AUDIENCES) is False
    assert RedisHealthProbe(failing_client).status() == "degraded"


def test_publisher_attempts_every_audience_after_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logfire, "exception", lambda *_, **__: None)
    calls: list[str] = []

    assert RealtimePublisher(FailingRedisClient(calls)).notify(EVENT_ID, ALL_AUDIENCES) is False
    assert calls == ["publish", "publish"]


@pytest.mark.parametrize(
    ("cause", "expected_audiences"),
    [
        ("participant_join", ("operator",)),
        ("participant_answer", ("operator",)),
        ("operator_mutation", ("participant", "operator")),
    ],
)
def test_invalidation_policy_limits_participant_behavior_notifications(
    cause: InvalidationCause,
    expected_audiences: tuple[RealtimeAudience, ...],
) -> None:
    assert audiences_for_invalidation(cause) == expected_audiences


class FakePubSub:
    def __init__(self, messages: Sequence[object]) -> None:
        self._messages = list(messages)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float | None,  # noqa: ASYNC109 - Redis APIのkeyword契約を模倣する。
    ) -> object:
        assert ignore_subscribe_messages is True
        assert timeout is None
        if not self._messages:
            await asyncio.Event().wait()
        return self._messages.pop(0)

    async def aclose(self) -> None:
        return None


class FakeAsyncRedis:
    async def aclose(self) -> None:
        return None


class AcknowledgingPubSub:
    def __init__(self, messages: Sequence[object]) -> None:
        self._messages = list(messages)
        self.subscribed_channels: list[str] = []
        self.get_message_calls: list[tuple[bool, float | None]] = []
        self.is_closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_channels.append(channel)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float | None,  # noqa: ASYNC109 - Redis APIのkeyword契約を模倣する。
    ) -> object:
        self.get_message_calls.append((ignore_subscribe_messages, timeout))
        if not self._messages:
            await asyncio.Event().wait()
        return self._messages.pop(0)

    async def aclose(self) -> None:
        self.is_closed = True


class FactoryRedisClient:
    def __init__(self, pubsub: AcknowledgingPubSub) -> None:
        self._pubsub = pubsub
        self.is_closed = False

    def pubsub(self) -> PubSub:
        return cast(PubSub, self._pubsub)

    async def aclose(self) -> None:
        self.is_closed = True


def install_async_redis_factory(
    monkeypatch: pytest.MonkeyPatch,
    pubsub: AcknowledgingPubSub,
) -> tuple[Settings, FactoryRedisClient]:
    settings = Settings(_env_file=None)
    client = FactoryRedisClient(pubsub)

    def from_pool(_: object) -> AsyncRedis:
        return cast(AsyncRedis, client)

    monkeypatch.setattr(AsyncRedis, "from_pool", staticmethod(from_pool))
    monkeypatch.setattr("quiz_app.realtime.redis_adapter.get_settings", lambda: settings)
    return settings, client


@pytest.mark.asyncio
async def test_subscription_factory_waits_for_ack_and_keeps_following_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = realtime_channel("participant", EVENT_ID)
    expected = StateInvalidatedMessage(
        type="state.invalidated",
        audience="participant",
        event_id=EVENT_ID,
    )
    pubsub = AcknowledgingPubSub(
        [
            {"type": "subscribe", "pattern": None, "channel": channel, "data": 1},
            {"data": expected.model_dump_json()},
        ]
    )
    settings, client = install_async_redis_factory(monkeypatch, pubsub)

    subscription = await RedisRealtimeSubscriptionFactory().subscribe("participant", EVENT_ID)

    assert pubsub.subscribed_channels == [channel]
    assert pubsub.get_message_calls == [(False, settings.redis_operation_timeout_seconds)]
    assert await subscription.next_message() == expected
    assert pubsub.get_message_calls[-1] == (True, None)
    await subscription.close()
    assert pubsub.is_closed is True
    assert client.is_closed is True


@pytest.mark.parametrize(
    "acknowledgement",
    [
        None,
        {"type": "subscribe", "pattern": None, "channel": "missing-data"},
        {
            "type": "subscribe",
            "pattern": None,
            "channel": "quiz:realtime:participant:wrong-event",
            "data": 1,
        },
    ],
)
@pytest.mark.asyncio
async def test_subscription_factory_rejects_invalid_ack_and_cleans_resources(
    acknowledgement: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = AcknowledgingPubSub([acknowledgement])
    settings, client = install_async_redis_factory(monkeypatch, pubsub)

    with pytest.raises(RealtimeUnavailableError):
        await RedisRealtimeSubscriptionFactory().subscribe("participant", EVENT_ID)

    assert pubsub.is_closed is True
    assert client.is_closed is True
    assert pubsub.get_message_calls == [(False, settings.redis_operation_timeout_seconds)]


@pytest.mark.asyncio
async def test_subscription_discards_cross_audience_and_malformed_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logfire, "warning", lambda *_, **__: None)
    wrong_audience = StateInvalidatedMessage(
        type="state.invalidated",
        audience="operator",
        event_id=EVENT_ID,
    ).model_dump_json()
    expected = StateInvalidatedMessage(
        type="state.invalidated",
        audience="participant",
        event_id=EVENT_ID,
    )
    pubsub = FakePubSub(
        [
            {"data": wrong_audience},
            {"data": '{"type":"state.invalidated","secret":"no"}'},
            {"data": expected.model_dump_json()},
        ]
    )
    subscription = RedisRealtimeSubscription(
        cast(AsyncRedis, FakeAsyncRedis()),
        cast(PubSub, pubsub),
        "participant",
        EVENT_ID,
    )

    assert await subscription.next_message() == expected
    await subscription.close()


def test_origin_must_match_exact_allowlist() -> None:
    settings = Settings(_env_file=None)

    assert is_allowed_origin("http://localhost:8080", settings) is True
    assert is_allowed_origin("http://localhost:8080.evil.example", settings) is False
    assert is_allowed_origin(None, settings) is False


def test_websocket_rejects_disallowed_origin(realtime_client: TestClient) -> None:
    app.dependency_overrides[get_state_stream_factory] = UnavailableSubscriptionFactory

    with (
        pytest.raises(WebSocketDisconnect) as error,
        realtime_client.websocket_connect(
            "/api/v1/participant/realtime",
            headers={"origin": "https://evil.example"},
        ),
    ):
        pass

    assert error.value.code == ORIGIN_REJECTED_CLOSE_CODE


def test_websocket_returns_polling_contract_then_closes_1013(
    realtime_client: TestClient,
) -> None:
    app.dependency_overrides[get_state_stream_factory] = UnavailableSubscriptionFactory

    with realtime_client.websocket_connect(
        "/api/v1/participant/realtime",
        headers={
            "origin": "http://localhost:8080",
            "cookie": f"{PARTICIPANT_SESSION_COOKIE_NAME}={PARTICIPANT_SESSION_FIXTURE}",
        },
    ) as websocket:
        assert websocket.receive_json() == {
            "type": "polling.required",
            "reason": "realtime_unavailable",
            "poll_after_ms": 5_000,
        }
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()

    assert error.value.code == REALTIME_UNAVAILABLE_CLOSE_CODE


@pytest.mark.parametrize(
    "failure",
    [
        SingleEventConfigurationError("single_event_configuration_required"),
        SQLAlchemyError("database_fixture_unavailable"),
        AudienceServiceUnavailableError("participant_recovery_deferred"),
    ],
)
def test_authenticated_websocket_fails_closed_when_fresh_verification_unavailable(
    realtime_client: TestClient,
    failure: Exception,
) -> None:
    class UnavailableParticipantAsyncService:
        async def authenticate_realtime(self, _token: str) -> VerifiedParticipantAuthentication:
            raise failure

    app.dependency_overrides[get_audience_async_service] = UnavailableParticipantAsyncService

    with (
        pytest.raises(WebSocketDisconnect) as error,
        realtime_client.websocket_connect(
            "/api/v1/participant/realtime",
            headers={
                "origin": "http://localhost:8080",
                "cookie": f"{PARTICIPANT_SESSION_COOKIE_NAME}={PARTICIPANT_SESSION_FIXTURE}",
            },
        ),
    ):
        pass

    assert error.value.code == REALTIME_UNAVAILABLE_CLOSE_CODE


def test_websocket_emits_only_its_audience_invalidation(
    realtime_client: TestClient,
) -> None:
    subscription = StubSubscription(
        StateInvalidatedMessage(
            type="state.invalidated",
            audience="participant",
            event_id=EVENT_ID,
        )
    )
    factory = StubSubscriptionFactory(subscription)
    app.dependency_overrides[get_state_stream_factory] = lambda: factory

    with realtime_client.websocket_connect(
        "/api/v1/participant/realtime",
        headers={
            "origin": "http://localhost:8080",
            "cookie": f"{PARTICIPANT_SESSION_COOKIE_NAME}={PARTICIPANT_SESSION_FIXTURE}",
        },
    ) as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "participant.state"
        assert snapshot["audience"] == "participant"
        assert snapshot["event_id"] == str(EVENT_ID)
        assert snapshot["sequence"] == 1
        assert snapshot["state"]["event"]["event_version"] == 1
        heartbeat = websocket.receive_json()
        assert heartbeat["type"] == "heartbeat"
        assert heartbeat["stream_id"] == snapshot["stream_id"]
        assert heartbeat["sequence"] == 2

    assert subscription.is_closed is True
    assert factory.audience == "participant"
    assert factory.event_id == EVENT_ID


def test_websocket_rejects_client_messages_with_distinct_close_code(
    realtime_client: TestClient,
) -> None:
    subscription = BlockingSubscription()
    app.dependency_overrides[get_state_stream_factory] = lambda: BlockingSubscriptionFactory(
        subscription
    )

    with realtime_client.websocket_connect(
        "/api/v1/participant/realtime",
        headers={
            "origin": "http://localhost:8080",
            "cookie": f"{PARTICIPANT_SESSION_COOKIE_NAME}={PARTICIPANT_SESSION_FIXTURE}",
        },
    ) as websocket:
        assert websocket.receive_json()["type"] == "participant.state"
        websocket.send_text("unexpected-client-message")
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()

    assert error.value.code == UNSUPPORTED_CLIENT_MESSAGE_CLOSE_CODE
    assert error.value.code != ORIGIN_REJECTED_CLOSE_CODE
    assert subscription.is_closed is True


@pytest.mark.parametrize("session_token", [None, "invalid-participant-session"])
def test_participant_websocket_rejects_missing_or_invalid_cookie(
    realtime_client: TestClient,
    session_token: str | None,
) -> None:
    app.dependency_overrides[get_audience_async_service] = StubParticipantAsyncRealtimeService
    app.dependency_overrides[get_state_stream_factory] = UnavailableSubscriptionFactory
    if session_token is not None:
        realtime_client.cookies.set(
            PARTICIPANT_SESSION_COOKIE_NAME,
            session_token,
            path="/api/v1/participant",
        )

    with (
        pytest.raises(WebSocketDisconnect) as error,
        realtime_client.websocket_connect(
            "/api/v1/participant/realtime",
            headers={"origin": "http://localhost:8080"},
        ),
    ):
        pass

    assert error.value.code == AUTH_REQUIRED_CLOSE_CODE


def test_participant_websocket_binds_subscription_to_principal_event(
    realtime_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_threadpool(*_args: object) -> None:
        raise AssertionError("participant_auth_must_not_use_operator_threadpool")

    monkeypatch.setattr("quiz_app.realtime.router.run_in_threadpool", reject_threadpool)
    app.dependency_overrides[get_audience_async_service] = StubParticipantAsyncRealtimeService
    subscription = StubSubscription(
        StateInvalidatedMessage(
            type="state.invalidated",
            audience="participant",
            event_id=EVENT_ID,
        )
    )
    factory = StubSubscriptionFactory(subscription)
    app.dependency_overrides[get_state_stream_factory] = lambda: factory
    realtime_client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME,
        PARTICIPANT_SESSION_FIXTURE,
        path="/api/v1/participant",
    )

    with realtime_client.websocket_connect(
        "/api/v1/participant/realtime",
        headers={"origin": "http://localhost:8080"},
    ) as websocket:
        assert websocket.receive_json()["audience"] == "participant"

    assert factory.audience == "participant"
    assert factory.event_id == EVENT_ID


@pytest.mark.parametrize("session_token", [None, "invalid-operator-session"])
def test_operator_websocket_rejects_missing_or_invalid_cookie(
    realtime_client: TestClient,
    session_token: str | None,
) -> None:
    app.dependency_overrides[get_operator_auth_service] = StubOperatorRealtimeAuthService
    app.dependency_overrides[get_state_stream_factory] = UnavailableSubscriptionFactory
    if session_token is not None:
        realtime_client.cookies.set(
            OPERATOR_SESSION_COOKIE_NAME,
            session_token,
            path="/api/v1/operator",
        )

    with (
        pytest.raises(WebSocketDisconnect) as error,
        realtime_client.websocket_connect(
            "/api/v1/operator/realtime",
            headers={"origin": "http://localhost:8080"},
        ),
    ):
        pass

    assert error.value.code == AUTH_REQUIRED_CLOSE_CODE


def test_operator_websocket_uses_authenticated_cookie_and_single_event(
    realtime_client: TestClient,
) -> None:
    app.dependency_overrides[get_operator_auth_service] = StubOperatorRealtimeAuthService
    subscription = StubSubscription(
        StateInvalidatedMessage(
            type="state.invalidated",
            audience="operator",
            event_id=EVENT_ID,
        )
    )
    factory = StubSubscriptionFactory(subscription)
    app.dependency_overrides[get_state_stream_factory] = lambda: factory
    realtime_client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        OPERATOR_SESSION_FIXTURE,
        path="/api/v1/operator",
    )

    with realtime_client.websocket_connect(
        "/api/v1/operator/realtime",
        headers={"origin": "http://localhost:8080"},
    ) as websocket:
        assert websocket.receive_json()["audience"] == "operator"

    assert factory.audience == "operator"
    assert factory.event_id == EVENT_ID


@pytest.mark.asyncio
async def test_outer_cancellation_cleans_all_websocket_tasks() -> None:
    baseline_tasks = asyncio.all_tasks()
    websocket = BlockingWebSocket()
    subscription = BlockingSubscription()
    serving_task = asyncio.create_task(
        _serve_states(
            cast(WebSocket, websocket),
            "participant",
            EVENT_ID,
            BlockingSubscriptionFactory(subscription),
            StubParticipantRealtimeAuthService().authenticate_realtime(PARTICIPANT_SESSION_FIXTURE),
            lambda: StubParticipantRealtimeAuthService().authenticate_realtime(
                PARTICIPANT_SESSION_FIXTURE
            ),
        )
    )
    await websocket.receive_started.wait()
    await subscription.started.wait()

    serving_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving_task
    await asyncio.sleep(0)

    assert websocket.is_receive_cancelled is True
    assert subscription.is_cancelled is True
    assert subscription.is_closed is True
    assert asyncio.all_tasks() <= baseline_tasks


class CommitRecordingMutationService:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def begin_preparation(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        self._calls.append("database_committed")
        return OperatorMutationResponse(
            operation="begin_preparation",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=round_id,
            round_version=request.expected_version + 1,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
        )


def test_operator_publish_occurs_after_committed_service_and_failure_keeps_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logfire, "exception", lambda *_, **__: None)
    calls: list[str] = []
    service = cast(OperatorMutationService, CommitRecordingMutationService(calls))
    publisher = RealtimePublisher(FailingRedisClient(calls))
    request = OperatorMutationRequest(
        expected_version=1,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    principal = OperatorPrincipal(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        username="demo-operator",
        role=AdminRole.OPERATOR,
        csrf_hash="stored",
    )

    response = begin_preparation(
        round_id=ROUND_ID,
        request=request,
        principal=principal,
        _=EVENT_ID,
        mutation_service=service,
        realtime_publisher=publisher,
    )

    assert response.event_id == EVENT_ID
    assert calls == ["database_committed", "publish", "publish"]


def test_removed_public_websocket_route_does_not_connect(realtime_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as error:
        with realtime_client.websocket_connect(
            "/api/v1/public/realtime", headers={"origin": "http://localhost:8080"}
        ):
            pass
    assert error.value.code == 1000


def test_removed_public_audience_is_rejected_by_message_contract() -> None:
    with pytest.raises(ValidationError):
        StateInvalidatedMessage.model_validate(
            {"type": "state.invalidated", "audience": "public", "event_id": EVENT_ID}
        )
