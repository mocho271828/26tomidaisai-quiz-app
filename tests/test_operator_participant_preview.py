from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from quiz_app.audience.contracts import (
    AudienceEventResponse,
    AudienceOptionResponse,
    AudienceQuestionResponse,
    ParticipantCommonLatestResultResponse,
    ParticipantCommonStateResponse,
    ParticipantCurrentRoundResponse,
    PublishedResultResponse,
)
from quiz_app.audience.projection import get_audience_projection_service
from quiz_app.config import OPERATOR_SESSION_COOKIE_NAME, PARTICIPANT_SESSION_COOKIE_NAME
from quiz_app.main import create_app
from quiz_app.models.enums import AdminRole, EventState, RoundState
from quiz_app.operator.auth_service import OperatorPrincipal, get_operator_auth_service
from quiz_app.operator.errors import OperatorSessionInvalidError
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.projection_budget import ProjectionReadDeferred
from quiz_app.realtime.state_cache import ProjectionSnapshot
from quiz_app.single_event import SingleEventConfigurationError, get_single_event_service

EVENT_ID = UUID(int=39)
ROUND_ID = UUID(int=40)
QUESTION_ID = UUID(int=41)
OPTION_ID = UUID(int=42)
PROJECTED_AT = datetime(2026, 10, 22, tzinfo=UTC)
PREVIEW_PATH = "/api/v1/operator/participant-preview"
SESSION_FIXTURE = "moderator-operator-session-fixture"


def _common_state(*, is_published: bool = False) -> ParticipantCommonStateResponse:
    question = AudienceQuestionResponse(
        question_id=QUESTION_ID,
        prompt="参加者に配信済みの問題",
        options=[AudienceOptionResponse(option_id=OPTION_ID, label="A", content="選択肢")],
    )
    latest_result = None
    if is_published:
        latest_result = ParticipantCommonLatestResultResponse(
            round_id=ROUND_ID,
            round_number=3,
            version=5,
            state_version=5,
            state=RoundState.REVEAL,
            open_at=None,
            standard_close_at=None,
            question=question,
            result=PublishedResultResponse(
                publication="Published", correct_option_id=OPTION_ID, distribution=None
            ),
        )
    return ParticipantCommonStateResponse(
        award_ceremony=None,
        projected_at=PROJECTED_AT,
        event=AudienceEventResponse(
            event_id=EVENT_ID,
            event_title="司会用プレビュー",
            event_state=EventState.IN_PROGRESS,
            event_version=9,
            start_delay_seconds=3,
            timing_basis="client_monotonic_v1",
            arrival_grace_ms=2000,
            clock_tolerance_ms=500,
            client_ahead_tolerance_ms=2500,
        ),
        current_round=ParticipantCurrentRoundResponse(
            round_id=ROUND_ID,
            round_number=3,
            version=5,
            state_version=5,
            state=RoundState.REVEAL if is_published else RoundState.READY_TO_REVEAL,
            open_at=None,
            standard_close_at=None,
            question=question,
        ),
        latest_result=latest_result,
        result_visibility=ResultVisibility(
            distribution=False, personal_ranking=False, overall_ranking=False
        ),
        latest_ranking_snapshot_version=None,
    )


class StubAuthService:
    def authenticate(self, session_token: str) -> OperatorPrincipal:
        if session_token != SESSION_FIXTURE:
            raise OperatorSessionInvalidError("invalid")
        return OperatorPrincipal(
            account_id=UUID(int=1),
            session_id=UUID(int=2),
            username="operator-fixture",
            role=AdminRole.OPERATOR,
            csrf_hash="csrf-hash-fixture",
        )


class StubSingleEventService:
    is_available = True

    def get_event_id(self) -> UUID:
        if not self.is_available:
            raise SingleEventConfigurationError("single_event_configuration_required")
        return EVENT_ID


class StubProjectionService:
    def __init__(self) -> None:
        self.state = _common_state()
        self.calls: list[tuple[UUID, bool]] = []
        self.is_available = True

    def get_common_snapshot(
        self, event_id: UUID, *, normal: bool = False
    ) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        self.calls.append((event_id, normal))
        if not self.is_available:
            raise ProjectionReadDeferred("projection_read_admission_expired")
        return ProjectionSnapshot(self.state, 1.0, 6.0)


@pytest_asyncio.fixture
async def preview_client() -> AsyncIterator[
    tuple[AsyncClient, StubProjectionService, StubSingleEventService]
]:
    application = create_app()
    projection = StubProjectionService()
    single_event = StubSingleEventService()
    application.dependency_overrides[get_operator_auth_service] = StubAuthService
    application.dependency_overrides[get_audience_projection_service] = lambda: projection
    application.dependency_overrides[get_single_event_service] = lambda: single_event
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client, projection, single_event


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["missing", "participant", "invalid_operator"])
async def test_preview_requires_valid_existing_operator_session_before_loading(
    preview_client: tuple[AsyncClient, StubProjectionService, StubSingleEventService],
    credential: str,
) -> None:
    client, projection, _ = preview_client
    if credential == "participant":
        client.cookies.set(PARTICIPANT_SESSION_COOKIE_NAME, SESSION_FIXTURE, path="/")
    elif credential == "invalid_operator":
        client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, "invalid-fixture", path="/")

    response = await client.get(PREVIEW_PATH)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "operator_authentication_required"
    assert projection.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("is_published", [False, True])
async def test_preview_uses_common_projection_and_preserves_its_publication_boundary(
    preview_client: tuple[AsyncClient, StubProjectionService, StubSingleEventService],
    is_published: bool,
) -> None:
    client, projection, _ = preview_client
    projection.state = _common_state(is_published=is_published)
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, SESSION_FIXTURE, path="/api/v1/operator")

    response = await client.get(PREVIEW_PATH)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "server-timing" not in response.headers
    assert projection.calls == [(EVENT_ID, True)]
    assert response.json() == projection.state.model_dump(mode="json")
    payload = response.json()
    assert not {"participant", "answer", "outcome", "ranking"} & payload.keys()
    if is_published:
        assert payload["latest_result"]["result"]["correct_option_id"] == str(OPTION_ID)
        assert payload["latest_result"]["result"]["distribution"] is None
    else:
        assert payload["latest_result"] is None
        assert "correct_option_id" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["event", "projection"])
async def test_preview_fails_closed_when_event_or_projection_is_unavailable(
    preview_client: tuple[AsyncClient, StubProjectionService, StubSingleEventService],
    unavailable: str,
) -> None:
    client, projection, single_event = preview_client
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, SESSION_FIXTURE, path="/api/v1/operator")
    if unavailable == "event":
        single_event.is_available = False
    else:
        projection.is_available = False

    response = await client.get(PREVIEW_PATH)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "current_round" not in response.json()
    if unavailable == "event":
        assert projection.calls == []


def test_preview_openapi_is_an_operator_authenticated_get_with_common_response() -> None:
    schema = create_app().openapi()
    route = schema["paths"][PREVIEW_PATH]
    assert set(route) == {"get"}
    operation = route["get"]
    assert operation["security"] == [{"OperatorSessionCookie": []}]
    assert "parameters" not in operation
    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ParticipantCommonStateResponse"
    }
    assert {"401", "403", "404", "503"} <= operation["responses"].keys()
