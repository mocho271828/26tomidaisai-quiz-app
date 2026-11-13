import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from quiz_app.audience.async_service import (
    CommittedParticipantAnswer,
    get_audience_async_service,
)
from quiz_app.audience.auth_service import (
    CreatedParticipantSession,
    ParticipantPrincipal,
    get_participant_auth_service,
)
from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AudienceEventResponse,
    AudienceOptionResponse,
    AudienceQuestionResponse,
    ParticipantAnswerResponse,
    ParticipantCurrentRoundResponse,
    ParticipantIdentityResponse,
    ParticipantStateResponse,
    PersonalResultDelivery,
    SubmitAnswerRequest,
    TimeResponse,
)
from quiz_app.audience.errors import (
    AudienceServiceUnavailableError,
    ParticipantIdExhaustedError,
    ParticipantSessionInvalidError,
)
from quiz_app.audience.projection import get_audience_projection_service
from quiz_app.config import PARTICIPANT_SESSION_COOKIE_NAME
from quiz_app.domain.errors import ClientReceiptDifferenceError, ClientSelectionTimeError
from quiz_app.domain.types import (
    AcceptedAnswer,
    SubmitAnswerCommand,
)
from quiz_app.main import app
from quiz_app.models.enums import EventState, RoundState
from quiz_app.observability.request_timing import observe_stage
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.answer_notifications import get_answer_notifications
from quiz_app.realtime.contracts import RealtimeAudience
from quiz_app.realtime.redis_adapter import (
    get_realtime_publisher,
)
from quiz_app.single_event import (
    SingleEventConfigurationError,
    get_single_event_service,
)

EVENT_ID = UUID("50000000-0000-4000-8000-000000000001")
OTHER_EVENT_ID = UUID("50000000-0000-4000-8000-000000000099")
PARTICIPANT_ID = UUID("50000000-0000-4000-8000-000000000002")
SESSION_ID = UUID("50000000-0000-4000-8000-000000000003")
ROUND_ID = UUID("50000000-0000-4000-8000-000000000004")
QUESTION_ID = UUID("50000000-0000-4000-8000-000000000005")
OPTION_ID = UUID("50000000-0000-4000-8000-000000000006")
ANSWER_ID = UUID("50000000-0000-4000-8000-000000000007")
CLIENT_ANSWER_ID = UUID("50000000-0000-4000-8000-000000000008")
CLIENT_JOIN_VALUE_FIXTURE = "A" * 43
PROJECTED_AT = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
NEW_SESSION_FIXTURE = "participant-session-value"
OLD_SESSION_FIXTURE = "old-participant-session-value"
PUBLIC_LABEL = "ABC"


def _principal() -> ParticipantPrincipal:
    return ParticipantPrincipal(
        participant_id=PARTICIPANT_ID,
        session_id=SESSION_ID,
        event_id=EVENT_ID,
        public_label=PUBLIC_LABEL,
        is_ranking_eligible=True,
    )


def _event() -> AudienceEventResponse:
    return AudienceEventResponse(
        timing_basis="server_received_v1",
        arrival_grace_ms=0,
        clock_tolerance_ms=0,
        client_ahead_tolerance_ms=0,
        event_id=EVENT_ID,
        event_title="[DEMO] 初回確認",
        event_state=EventState.IN_PROGRESS,
        event_version=3,
        start_delay_seconds=3,
    )


def _participant_round() -> ParticipantCurrentRoundResponse:
    return ParticipantCurrentRoundResponse(
        round_id=ROUND_ID,
        round_number=1,
        version=2,
        state_version=8,
        state=RoundState.SCHEDULED,
        open_at=PROJECTED_AT + timedelta(seconds=5),
        standard_close_at=PROJECTED_AT + timedelta(seconds=10),
        question=AudienceQuestionResponse(
            question_id=QUESTION_ID,
            prompt="[DEMO] 2 + 2 は？",
            options=[AudienceOptionResponse(option_id=OPTION_ID, label="B", content="4")],
        ),
    )


class StubParticipantAuthService:
    def __init__(self) -> None:
        self.existing_session_token: str | None = None
        self.touched_principal: ParticipantPrincipal | None = None

    def join(
        self,
        event_id: UUID,
        client_join_token: str,
        existing_session_token: str | None,
    ) -> CreatedParticipantSession:
        assert event_id == EVENT_ID
        assert client_join_token == CLIENT_JOIN_VALUE_FIXTURE
        self.existing_session_token = existing_session_token
        return CreatedParticipantSession(
            principal=_principal(),
            session_token=NEW_SESSION_FIXTURE,
            expires_at=PROJECTED_AT + timedelta(hours=12),
        )

    def authenticate(self, session_token: str) -> ParticipantPrincipal:
        if session_token != NEW_SESSION_FIXTURE:
            raise ParticipantSessionInvalidError("invalid")
        return _principal()

    def touch(self, principal: ParticipantPrincipal) -> None:
        self.touched_principal = principal


class UnavailableParticipantAuthService(StubParticipantAuthService):
    def join(
        self,
        _: UUID,
        client_join_token: str,
        existing_session_token: str | None,
    ) -> CreatedParticipantSession:
        del client_join_token, existing_session_token
        raise AudienceServiceUnavailableError("database_clock_unavailable")


class NonUtcExpiryParticipantAuthService(StubParticipantAuthService):
    def join(
        self,
        event_id: UUID,
        client_join_token: str,
        existing_session_token: str | None,
    ) -> CreatedParticipantSession:
        created = super().join(
            event_id,
            client_join_token,
            existing_session_token,
        )
        return CreatedParticipantSession(
            principal=created.principal,
            session_token=created.session_token,
            expires_at=created.expires_at.astimezone(timezone(timedelta(hours=9))),
        )


class IdExhaustedParticipantAuthService(StubParticipantAuthService):
    def join(
        self,
        _: UUID,
        client_join_token: str,
        existing_session_token: str | None,
    ) -> CreatedParticipantSession:
        del client_join_token, existing_session_token
        raise ParticipantIdExhaustedError("participant_id_exhausted")


class StubAudienceProjectionService:
    def get_participant_state(
        self,
        _: ParticipantPrincipal,
    ) -> ParticipantStateResponse:
        return ParticipantStateResponse(
            award_ceremony=None,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
            latest_ranking_snapshot_version=None,
            personal_result_delivery=PersonalResultDelivery(status="none", revision=None),
            projected_at=PROJECTED_AT,
            event=_event(),
            participant=ParticipantIdentityResponse(
                public_label=PUBLIC_LABEL,
                is_ranking_eligible=True,
            ),
            current_round=_participant_round(),
            latest_result=None,
        )

    def get_answer_status(
        self,
        principal: ParticipantPrincipal,
        client_answer_id: UUID,
    ) -> AcceptedAnswerStatusResponse:
        assert principal.participant_id == PARTICIPANT_ID
        assert client_answer_id == CLIENT_ANSWER_ID
        return AcceptedAnswerStatusResponse(
            status="AcceptedLocked",
            answer=ParticipantAnswerResponse(
                timing_basis="server_received_v1",
                client_elapsed_ms=None,
                answer_id=ANSWER_ID,
                round_id=ROUND_ID,
                option_id=OPTION_ID,
                client_answer_id=CLIENT_ANSWER_ID,
                received_at=PROJECTED_AT,
            ),
        )

    def get_server_time(self) -> datetime:
        return PROJECTED_AT


class StubEmptyRoundProjectionService(StubAudienceProjectionService):
    def get_participant_state(
        self,
        _: ParticipantPrincipal,
    ) -> ParticipantStateResponse:
        return ParticipantStateResponse(
            award_ceremony=None,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
            latest_ranking_snapshot_version=None,
            personal_result_delivery=PersonalResultDelivery(status="none", revision=None),
            projected_at=PROJECTED_AT,
            event=_event(),
            participant=ParticipantIdentityResponse(
                public_label=PUBLIC_LABEL,
                is_ranking_eligible=True,
            ),
            current_round=None,
            latest_result=None,
        )


class StubNullableTimingProjectionService(StubAudienceProjectionService):
    def get_participant_state(
        self,
        _: ParticipantPrincipal,
    ) -> ParticipantStateResponse:
        return ParticipantStateResponse(
            award_ceremony=None,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
            latest_ranking_snapshot_version=None,
            personal_result_delivery=PersonalResultDelivery(status="none", revision=None),
            projected_at=PROJECTED_AT,
            event=_event(),
            participant=ParticipantIdentityResponse(
                public_label=PUBLIC_LABEL,
                is_ranking_eligible=True,
            ),
            current_round=ParticipantCurrentRoundResponse(
                round_id=ROUND_ID,
                round_number=1,
                version=1,
                state_version=1,
                state=RoundState.PREPARING_ASSETS,
                open_at=None,
                standard_close_at=None,
                question=AudienceQuestionResponse(
                    question_id=QUESTION_ID,
                    prompt="[DEMO] 2 + 2 は？",
                    options=[
                        AudienceOptionResponse(
                            option_id=OPTION_ID,
                            label="B",
                            content="4",
                        )
                    ],
                ),
            ),
            latest_result=None,
        )


class StubQuizDomainService:
    def __init__(self) -> None:
        self.answer_command: SubmitAnswerCommand | None = None

    def submit_answer(self, command: SubmitAnswerCommand) -> AcceptedAnswer:
        self.answer_command = command
        return AcceptedAnswer(
            timing_basis="client_monotonic_v1",
            client_elapsed_ms=command.client_elapsed_ms,
            answer_id=ANSWER_ID,
            round_id=command.round_id,
            option_id=command.option_id,
            client_answer_id=command.client_answer_id,
            received_at=PROJECTED_AT,
            is_replay=False,
        )


class StubRealtimePublisher:
    def notify(
        self, _: UUID, __: tuple[RealtimeAudience, ...], *, cause: str = "operator_mutation"
    ) -> bool:
        assert cause == "participant_join"
        return True


class StubAnswerNotifications:
    def enqueue(self, _: UUID) -> bool:
        return True


class StubSingleEventService:
    def get_event_id(self) -> UUID:
        return EVENT_ID

    def require_round(self, round_id: UUID) -> UUID:
        assert round_id == ROUND_ID
        return EVENT_ID


class MisconfiguredSingleEventService(StubSingleEventService):
    def get_event_id(self) -> UUID:
        raise SingleEventConfigurationError("single_event_configuration_required")


class OtherSingleEventService(StubSingleEventService):
    def get_event_id(self) -> UUID:
        return OTHER_EVENT_ID


class StubAudienceAsyncService:
    """HTTP契約testでは既存のservice fixtureをasync入口へ接続する。"""

    def __init__(self, domain: StubQuizDomainService) -> None:
        self._domain = domain

    @staticmethod
    def _event_service() -> StubSingleEventService:
        return cast(StubSingleEventService, app.dependency_overrides[get_single_event_service]())

    @staticmethod
    def _projection() -> StubAudienceProjectionService:
        return cast(
            StubAudienceProjectionService,
            app.dependency_overrides[get_audience_projection_service](),
        )

    @classmethod
    def _principal(cls, session_token: str | None) -> ParticipantPrincipal:
        event_id = cls._event_service().get_event_id()
        if session_token is None:
            raise ParticipantSessionInvalidError("participant_session_missing")
        authentication = cast(
            StubParticipantAuthService, app.dependency_overrides[get_participant_auth_service]()
        )
        principal = authentication.authenticate(session_token)
        if principal.event_id != event_id:
            raise ParticipantSessionInvalidError("participant_session_event_mismatch")
        return principal

    async def get_participant_state(self, session_token: str | None) -> ParticipantStateResponse:
        with observe_stage("state_projection"):
            return self._projection().get_participant_state(self._principal(session_token))

    async def get_answer_status(
        self, session_token: str | None, client_answer_id: UUID
    ) -> AcceptedAnswerStatusResponse:
        with observe_stage("status_projection"):
            return self._projection().get_answer_status(
                self._principal(session_token), client_answer_id
            )

    async def get_server_time(self) -> TimeResponse:
        self._event_service().get_event_id()
        return TimeResponse(
            server_time=self._projection().get_server_time(), shared_sample_age_ms=0
        )

    async def submit_answer(
        self, session_token: str | None, round_id: UUID, request: SubmitAnswerRequest
    ) -> CommittedParticipantAnswer:
        principal = self._principal(session_token)
        self._event_service().require_round(round_id)
        with observe_stage("answer_transaction"):
            answer = self._domain.submit_answer(
                SubmitAnswerCommand(
                    client_elapsed_ms=request.client_elapsed_ms,
                    participant_id=principal.participant_id,
                    round_id=round_id,
                    round_version=request.round_version,
                    option_id=request.option_id,
                    client_answer_id=request.client_answer_id,
                )
            )
        return CommittedParticipantAnswer(event_id=principal.event_id, answer=answer)


@pytest_asyncio.fixture
async def audience_client() -> AsyncIterator[
    tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService]
]:
    auth_service = StubParticipantAuthService()
    domain_service = StubQuizDomainService()
    app.dependency_overrides[get_participant_auth_service] = lambda: auth_service
    app.dependency_overrides[get_audience_projection_service] = StubAudienceProjectionService
    app.dependency_overrides[get_audience_async_service] = lambda: StubAudienceAsyncService(
        domain_service
    )
    app.dependency_overrides[get_realtime_publisher] = StubRealtimePublisher
    app.dependency_overrides[get_answer_notifications] = StubAnswerNotifications
    app.dependency_overrides[get_single_event_service] = StubSingleEventService
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client, auth_service, domain_service
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_token", [None, OLD_SESSION_FIXTURE])
async def test_join_sets_scoped_cookie_without_returning_session_token(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
    session_token: str | None,
) -> None:
    client, auth_service, _ = audience_client
    if session_token is not None:
        client.cookies.set(
            PARTICIPANT_SESSION_COOKIE_NAME,
            session_token,
            path="/api/v1/participant",
        )

    response = await client.post(
        "/api/v1/participant/join",
        json={"client_join_token": CLIENT_JOIN_VALUE_FIXTURE},
    )

    assert response.status_code == 200
    assert auth_service.existing_session_token == session_token
    assert response.json()["participant"]["public_label"] == PUBLIC_LABEL
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "expires=Sat, 08 Aug 2026 22:00:00 GMT" in cookie
    assert "Max-Age" not in cookie
    assert "Path=/api/v1/participant" in cookie
    assert "SameSite=strict" in cookie
    assert NEW_SESSION_FIXTURE not in response.text
    assert "result" not in response.json()["current_round"]
    assert response.json()["latest_result"] is None


@pytest.mark.asyncio
async def test_join_id_exhaustion_has_dedicated_response(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    app.dependency_overrides[get_participant_auth_service] = IdExhaustedParticipantAuthService

    response = await client.post(
        "/api/v1/participant/join",
        json={"client_join_token": CLIENT_JOIN_VALUE_FIXTURE},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "participant_id_exhausted",
        "message": (
            "このイベントで割り当て可能な参加者IDをすべて使用しました。運営にお問い合わせください。"
        ),
    }
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_join_rejects_client_specified_public_label(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client

    response = await client.post(
        "/api/v1/participant/join",
        json={"client_join_token": CLIENT_JOIN_VALUE_FIXTURE, "public_label": PUBLIC_LABEL},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_database_clock_failure_returns_service_unavailable(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    app.dependency_overrides[get_participant_auth_service] = UnavailableParticipantAuthService

    response = await client.post(
        "/api/v1/participant/join",
        json={"client_join_token": CLIENT_JOIN_VALUE_FIXTURE},
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "audience_service_unavailable",
        "message": "一時的に要求を完了できません。時間をおいて再試行してください。",
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_event_dependent_api_fails_closed_when_single_event_is_not_configured(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    app.dependency_overrides[get_single_event_service] = MisconfiguredSingleEventService

    response = await client.get("/api/v1/time")

    assert response.status_code == 503
    assert response.json()["code"] == "domain_temporarily_unavailable"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_join_normalizes_non_utc_database_expiry_for_cookie(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    app.dependency_overrides[get_participant_auth_service] = NonUtcExpiryParticipantAuthService

    response = await client.post(
        "/api/v1/participant/join",
        json={"client_join_token": CLIENT_JOIN_VALUE_FIXTURE},
    )

    assert response.status_code == 200
    assert "expires=Sat, 08 Aug 2026 22:00:00 GMT" in response.headers["set-cookie"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/public/state", "/api/v1/participant/id-candidate"])
async def test_removed_read_routes_do_not_authenticate_or_query(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
    path: str,
) -> None:
    client, auth, _ = audience_client
    response = await client.get(path)
    assert response.status_code == 404
    assert auth.touched_principal is None
    assert path not in app.openapi()["paths"]
    assert "ParticipantIdCandidateResponse" not in app.openapi()["components"]["schemas"]


@pytest.mark.asyncio
async def test_answer_status_uses_authenticated_participant_only(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, auth_service, _ = audience_client
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME,
        NEW_SESSION_FIXTURE,
        path="/api/v1/participant",
    )

    response = await client.get(
        f"/api/v1/participant/answers/{CLIENT_ANSWER_ID}/status",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "AcceptedLocked"
    assert response.json()["answer"]["client_answer_id"] == str(CLIENT_ANSWER_ID)
    assert response.headers["cache-control"] == "no-store"
    assert auth_service.touched_principal is None

    malformed = await client.get("/api/v1/participant/answers/not-a-uuid/status")

    assert malformed.status_code == 422
    assert malformed.json() == {
        "code": "request_validation_failed",
        "message": "入力内容を確認してください。",
    }
    assert "not-a-uuid" not in malformed.text


@pytest.mark.asyncio
async def test_participant_session_cannot_cross_the_single_event_boundary(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    app.dependency_overrides[get_single_event_service] = OtherSingleEventService
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME,
        NEW_SESSION_FIXTURE,
        path="/api/v1/participant",
    )

    response = await client.get("/api/v1/participant/session")

    assert response.status_code == 401
    assert response.json()["code"] == "participant_authentication_required"


@pytest.mark.asyncio
async def test_nullable_contract_fields_remain_on_wire(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME,
        NEW_SESSION_FIXTURE,
        path="/api/v1/participant",
    )
    app.dependency_overrides[get_audience_projection_service] = StubEmptyRoundProjectionService

    empty_participant = await client.get("/api/v1/participant/session")

    assert empty_participant.json()["current_round"] is None
    assert empty_participant.json()["latest_result"] is None
    assert empty_participant.headers["cache-control"] == "no-store"
    assert "answer" not in empty_participant.json()
    assert "ranking" not in empty_participant.json()

    app.dependency_overrides[get_audience_projection_service] = StubNullableTimingProjectionService
    preparing = await client.get("/api/v1/participant/session")

    assert preparing.json()["current_round"]["open_at"] is None
    assert preparing.json()["current_round"]["standard_close_at"] is None


@pytest.mark.asyncio
async def test_answer_identity_comes_only_from_session(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, domain_service = audience_client
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME,
        NEW_SESSION_FIXTURE,
        path="/api/v1/participant",
    )
    request = {
        "client_elapsed_ms": 1000,
        "round_version": 2,
        "option_id": str(OPTION_ID),
        "client_answer_id": str(CLIENT_ANSWER_ID),
    }

    rejected = await client.post(
        f"/api/v1/participant/rounds/{ROUND_ID}/answers",
        json={**request, "participant_id": str(UUID(int=99))},
    )
    accepted = await client.post(
        f"/api/v1/participant/rounds/{ROUND_ID}/answers",
        json=request,
    )

    assert rejected.status_code == 422
    assert accepted.status_code == 200
    assert domain_service.answer_command is not None
    assert domain_service.answer_command.participant_id == PARTICIPANT_ID
    assert domain_service.answer_command.client_elapsed_ms == 1000
    assert accepted.json()["client_elapsed_ms"] == 1000
    assert accepted.json()["timing_basis"] == "client_monotonic_v1"
    assert "is_correct" not in accepted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ClientSelectionTimeError, ClientReceiptDifferenceError])
async def test_timing_rejection_uses_safe_message_without_claiming_tampering(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[ClientSelectionTimeError] | type[ClientReceiptDifferenceError],
) -> None:
    client, _, domain_service = audience_client

    def reject(_command: SubmitAnswerCommand) -> AcceptedAnswer:
        raise error_type("private-time-value")

    monkeypatch.setattr(domain_service, "submit_answer", reject)
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME, NEW_SESSION_FIXTURE, path="/api/v1/participant"
    )
    response = await client.post(
        f"/api/v1/participant/rounds/{ROUND_ID}/answers",
        json={
            "round_version": 2,
            "option_id": str(OPTION_ID),
            "client_answer_id": str(CLIENT_ANSWER_ID),
            "client_elapsed_ms": 1000,
        },
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": "state_conflict",
        "message": "時間の確認ができず、回答を受け付けられませんでした。",
    }


def test_openapi_requires_official_time_and_timing_basis() -> None:
    schemas = app.openapi()["components"]["schemas"]
    request = schemas["SubmitAnswerRequest"]
    assert "client_elapsed_ms" in request["required"]
    assert request["properties"]["client_elapsed_ms"] == {
        "type": "integer",
        "exclusiveMaximum": 10000.0,
        "minimum": 0.0,
        "title": "Client Elapsed Ms",
    }
    for model in ("AnswerReceiptResponse", "ParticipantAnswerResponse"):
        assert {"timing_basis", "client_elapsed_ms"} <= set(schemas[model]["required"])
    assert "timing_basis" in schemas["ScoredParticipantOutcomeResponse"]["required"]
    event = schemas["AudienceEventResponse"]
    assert "client_ahead_tolerance_ms" in event["required"]
    assert event["properties"]["client_ahead_tolerance_ms"]["type"] == "integer"
    assert event["properties"]["client_ahead_tolerance_ms"]["minimum"] == 0


@pytest.mark.asyncio
async def test_answer_and_recovery_timing_headers_preserve_the_response_contract(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME,
        NEW_SESSION_FIXTURE,
        path="/api/v1/participant",
    )
    accepted = await client.post(
        f"/api/v1/participant/rounds/{ROUND_ID}/answers",
        json={
            "client_elapsed_ms": 1000,
            "round_version": 2,
            "option_id": str(OPTION_ID),
            "client_answer_id": str(CLIENT_ANSWER_ID),
        },
    )
    recovered = await client.get(f"/api/v1/participant/answers/{CLIENT_ANSWER_ID}/status")
    state = await client.get("/api/v1/participant/session")

    for response, stage in (
        (accepted, "answer_transaction"),
        (recovered, "status_projection"),
        (state, "state_projection"),
    ):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        header = response.headers["server-timing"]
        for expected_stage in (
            "api_total",
            "pre_handler",
            "dispatch_wait",
            "handler",
            "response_build",
            stage,
        ):
            assert f"{expected_stage};dur=" in header
        assert "thread_wait;dur=" not in header
        assert str(CLIENT_ANSWER_ID) not in header
        assert NEW_SESSION_FIXTURE not in header
        assert "server_timing" not in response.json()
    assert accepted.json()["answer_id"] == str(ANSWER_ID)
    assert recovered.json()["status"] == "AcceptedLocked"


@pytest.mark.asyncio
async def test_answer_receipt_waits_only_for_commit_then_enqueues_best_effort(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, domain_service = audience_client
    committed = asyncio.Event()
    entered = asyncio.Event()
    calls: list[str] = []

    class DelayedCommitService(StubAudienceAsyncService):
        async def submit_answer(
            self, session_token: str | None, round_id: UUID, request: SubmitAnswerRequest
        ) -> CommittedParticipantAnswer:
            entered.set()
            await committed.wait()
            receipt = await super().submit_answer(session_token, round_id, request)
            calls.append("commit_acknowledged")
            return receipt

    class UnavailableNotifications:
        def enqueue(self, event_id: UUID) -> bool:
            assert event_id == EVENT_ID
            calls.append("enqueued")
            return False

    app.dependency_overrides[get_audience_async_service] = lambda: DelayedCommitService(
        domain_service
    )
    app.dependency_overrides[get_answer_notifications] = UnavailableNotifications
    client.cookies.set(
        PARTICIPANT_SESSION_COOKIE_NAME, NEW_SESSION_FIXTURE, path="/api/v1/participant"
    )
    pending = asyncio.create_task(
        client.post(
            f"/api/v1/participant/rounds/{ROUND_ID}/answers",
            json={
                "client_elapsed_ms": 1000,
                "round_version": 2,
                "option_id": str(OPTION_ID),
                "client_answer_id": str(CLIENT_ANSWER_ID),
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        assert calls == []
        assert not pending.done()
        committed.set()
        response = await asyncio.wait_for(pending, timeout=0.5)
    finally:
        committed.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert response.status_code == 200
    assert response.json()["answer_id"] == str(ANSWER_ID)
    assert calls == ["commit_acknowledged", "enqueued"]


@pytest.mark.asyncio
async def test_time_reports_async_timing_without_changing_clock_response(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client
    response = await client.get("/api/v1/time")
    assert response.status_code == 200
    assert response.json() == {
        "server_time": PROJECTED_AT.isoformat().replace("+00:00", "Z"),
        "shared_sample_age_ms": 0,
    }
    assert response.headers["cache-control"] == "no-store"
    assert "api_total;dur=" in response.headers["server-timing"]
    assert "dispatch_wait;dur=" in response.headers["server-timing"]
    assert "thread_wait;dur=" not in response.headers["server-timing"]


@pytest.mark.asyncio
async def test_participant_session_requires_cookie_and_openapi_matches_contract(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
) -> None:
    client, _, _ = audience_client

    unauthorized = await client.get("/api/v1/participant/session")
    time_response = await client.get("/api/v1/time")
    schema = app.openapi()
    session_operation = schema["paths"]["/api/v1/participant/session"]["get"]
    answer_status_operation = schema["paths"][
        "/api/v1/participant/answers/{client_answer_id}/status"
    ]["get"]
    submit_schema = schema["components"]["schemas"]["SubmitAnswerRequest"]
    participant_current_schema = schema["components"]["schemas"]["ParticipantCurrentRoundResponse"]
    participant_latest_schema = schema["components"]["schemas"][
        "ParticipantCommonLatestResultResponse"
    ]
    question_schema = schema["components"]["schemas"]["AudienceQuestionResponse"]
    join_schema = schema["components"]["schemas"]["ParticipantJoinRequest"]

    assert unauthorized.status_code == 401
    assert time_response.headers["cache-control"] == "no-store"
    assert session_operation["security"] == [{"ParticipantSessionCookie": []}]
    assert answer_status_operation["security"] == [{"ParticipantSessionCookie": []}]
    answer_operation = schema["paths"]["/api/v1/participant/rounds/{round_id}/answers"]["post"]
    for operation in (answer_operation, answer_status_operation):
        for response_status in ("200", "401", "409", "422", "503"):
            headers = operation["responses"][response_status]["headers"]
            assert headers["X-Quiz-Request-Id"]["schema"]["pattern"] == "^[0-9a-f]{32}$"
            assert headers["X-Quiz-Answer-Trace"]["schema"]["pattern"] == "^[0-9a-f]{64}$"
            assert not headers["X-Quiz-Answer-Trace"].get("required", False)
    assert "headers" not in session_operation["responses"]["200"]
    assert "requestBody" not in answer_status_operation
    assert "participant_id" not in submit_schema["properties"]
    for timeline_schema in (
        participant_current_schema,
        participant_latest_schema,
    ):
        assert "round_number" in timeline_schema["required"]
        assert timeline_schema["properties"]["round_number"]["type"] == "integer"
        assert timeline_schema["properties"]["round_number"]["minimum"] == 1
    assert "result" not in participant_current_schema["properties"]
    assert "selected_option_id" not in participant_latest_schema["properties"]
    assert (
        "selected_option_id" in schema["components"]["schemas"]["PersonalResultPayload"]["required"]
    )
    assert set(question_schema["required"]) == {"question_id", "prompt", "options"}
    assert not {"image_url", "image_alt"} & question_schema["properties"].keys()
    assert join_schema["required"] == ["client_join_token"]
    assert set(join_schema["properties"]) == {"client_join_token"}
    assert join_schema["additionalProperties"] is False
    assert {
        "/api/v1/participant/join",
        "/api/v1/participant/rounds/{round_id}/answers",
        "/api/v1/participant/answers/{client_answer_id}/status",
        "/api/v1/time",
    } <= schema["paths"].keys()
    assert "/api/v1/participant/events/{event_slug}/join" not in schema["paths"]
    assert "/api/v1/public/events/{event_slug}" not in schema["paths"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/api/v1/participant/session/heartbeat", f"/api/v1/participant/rounds/{ROUND_ID}/readiness"],
)
async def test_removed_activity_routes_do_not_authenticate_or_write(
    audience_client: tuple[AsyncClient, StubParticipantAuthService, StubQuizDomainService],
    path: str,
) -> None:
    client, auth, _ = audience_client
    response = await client.post(path, json={"round_version": 1, "is_ready": True})
    assert response.status_code == 404
    assert auth.touched_principal is None
    assert path not in app.openapi()["paths"]
