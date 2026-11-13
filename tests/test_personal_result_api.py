"""本人結果HTTP境界のCookie委譲・版・公開範囲・縮退応答を確認する。"""

from typing import Literal
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from quiz_app.audience.async_service import get_audience_async_service
from quiz_app.audience.contracts import (
    PersonalResultDelivery,
    PersonalResultPayload,
    PersonalResultResponse,
    PersonalResultRevision,
    ScoredParticipantOutcomeResponse,
)
from quiz_app.audience.error_handlers import install_audience_error_handlers
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.audience.router import router
from quiz_app.config import PARTICIPANT_SESSION_COOKIE_NAME
from quiz_app.models.enums import SpeedCohort
from quiz_app.quiz_configuration import ResultVisibility

EVENT_ID, ROUND_ID = UUID(int=1), UUID(int=2)


def test_http_and_websocket_openapi_share_required_personal_score_details() -> None:
    from quiz_app.main import app

    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    outcome = schemas["ScoredParticipantOutcomeResponse"]
    assert {"base_points", "speed_points", "elapsed_ms"} <= set(outcome["required"])
    for field in ("base_points", "speed_points"):
        assert outcome["properties"][field]["type"] == "integer"
        assert outcome["properties"][field]["minimum"] == 0
    assert outcome["properties"]["elapsed_ms"]["anyOf"] == [
        {"minimum": 0, "type": "integer"},
        {"type": "null"},
    ]
    operation = schema["paths"]["/api/v1/participant/result"]["get"]
    response_reference = {"$ref": "#/components/schemas/PersonalResultResponse"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == (
        response_reference
    )
    assert schemas["ParticipantResultMessage"]["properties"]["result"] == response_reference
    assert not {"base_points", "speed_points", "elapsed_ms", "outcome"} & set(
        schemas["PublishedResultResponse"]["properties"]
    )


def result_response(
    value: int,
    *,
    state: Literal["ready", "pending", "none", "unavailable"] = "ready",
    visible: bool = True,
) -> PersonalResultResponse:
    revision = (
        None
        if state == "none"
        else PersonalResultRevision(
            event_id=EVENT_ID,
            event_version=3 if visible else 4,
            result_round_id=ROUND_ID,
            result_round_version=2,
            ranking_snapshot_version=None,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=visible, overall_ranking=False
            ),
        )
    )
    return PersonalResultResponse(
        delivery=PersonalResultDelivery(status=state, revision=revision),
        result=PersonalResultPayload(
            selected_option_id=UUID(int=value),
            outcome=ScoredParticipantOutcomeResponse(
                timing_basis="server_received_v1",
                status="Scored",
                is_correct=True,
                base_points=value,
                speed_points=0,
                elapsed_ms=value * 10,
                total_points=value,
                scoring_policy_version="fixture",
                speed_cohort=SpeedCohort.CORRECT,
                speed_rank=1 if visible else None,
                speed_cohort_size=2 if visible else None,
            ),
            ranking=None,
        )
        if state == "ready"
        else None,
    )


class StubPersonalResultService:
    def __init__(self) -> None:
        self.response_by_token = {
            "fixture-one": result_response(100),
            "fixture-two": result_response(200),
        }
        self.tokens: list[str | None] = []
        self.unavailable = False

    async def get_participant_result(self, session_token: str | None) -> PersonalResultResponse:
        self.tokens.append(session_token)
        if session_token not in self.response_by_token:
            raise ParticipantSessionInvalidError("fixture_invalid")
        if self.unavailable:
            raise AudienceServiceUnavailableError("fixture_deferred")
        assert session_token is not None
        return self.response_by_token[session_token]


def application(service: StubPersonalResultService) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    install_audience_error_handlers(app)
    app.dependency_overrides[get_audience_async_service] = lambda: service
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "invalid-fixture"])
async def test_personal_result_passes_missing_or_invalid_cookie_to_authentication(
    token: str | None,
) -> None:
    service = StubPersonalResultService()
    cookies = {} if token is None else {PARTICIPANT_SESSION_COOKIE_NAME: token}
    async with AsyncClient(
        transport=ASGITransport(app=application(service)), base_url="https://test", cookies=cookies
    ) as client:
        response = await client.get("/api/v1/participant/result")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "participant_authentication_required"
    assert service.tokens == [token]


@pytest.mark.asyncio
async def test_personal_result_identity_comes_from_cookie_and_response_is_one_person() -> None:
    service = StubPersonalResultService()
    async with AsyncClient(
        transport=ASGITransport(app=application(service)),
        base_url="https://test",
        cookies={PARTICIPANT_SESSION_COOKIE_NAME: "fixture-one"},
    ) as client:
        first = await client.get("/api/v1/participant/result?participant_id=fixture-two")
        client.cookies.set(PARTICIPANT_SESSION_COOKIE_NAME, "fixture-two")
        second = await client.get("/api/v1/participant/result")
    assert first.status_code == second.status_code == 200
    for response, value in ((first, 100), (second, 200)):
        parsed = PersonalResultResponse.model_validate(response.json())
        assert parsed.result is not None and parsed.result.outcome is not None
        assert isinstance(parsed.result.outcome, ScoredParticipantOutcomeResponse)
        assert parsed.result.outcome.total_points == value
        assert parsed.result.outcome.base_points == value
        assert parsed.result.outcome.speed_points == 0
        assert parsed.result.outcome.elapsed_ms == value * 10
        assert response.headers["cache-control"] == "no-store"
        assert set(response.json()) == {"delivery", "result"}
        assert "fixture-one" not in response.text and "fixture-two" not in response.text
    assert service.tokens == ["fixture-one", "fixture-two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "none", "unavailable"])
async def test_personal_result_pending_and_unavailable_do_not_fabricate_unanswered(
    state: Literal["pending", "none", "unavailable"],
) -> None:
    service = StubPersonalResultService()
    service.response_by_token["fixture-one"] = result_response(100, state=state)
    async with AsyncClient(
        transport=ASGITransport(app=application(service)),
        base_url="https://test",
        cookies={PARTICIPANT_SESSION_COOKIE_NAME: "fixture-one"},
    ) as client:
        response = await client.get("/api/v1/participant/result")
    assert response.status_code == 200
    assert response.json()["delivery"]["status"] == state
    assert response.json()["result"] is None


@pytest.mark.asyncio
async def test_personal_result_hidden_revision_omits_rank_and_preserves_score() -> None:
    service = StubPersonalResultService()
    service.response_by_token["fixture-one"] = result_response(100, visible=False)
    async with AsyncClient(
        transport=ASGITransport(app=application(service)),
        base_url="https://test",
        cookies={PARTICIPANT_SESSION_COOKIE_NAME: "fixture-one"},
    ) as client:
        response = await client.get("/api/v1/participant/result")
    payload = response.json()
    assert payload["delivery"]["revision"]["event_version"] == 4
    assert payload["delivery"]["revision"]["result_visibility"]["personal_ranking"] is False
    assert payload["result"]["ranking"] is None
    assert payload["result"]["outcome"]["speed_rank"] is None
    assert payload["result"]["outcome"]["speed_cohort_size"] is None
    assert payload["result"]["outcome"]["total_points"] == 100
    assert payload["result"]["outcome"]["base_points"] == 100
    assert payload["result"]["outcome"]["speed_points"] == 0
    assert payload["result"]["outcome"]["elapsed_ms"] == 1000


@pytest.mark.asyncio
async def test_personal_result_overload_has_bounded_retry_hint_without_private_error() -> None:
    service = StubPersonalResultService()
    service.unavailable = True
    async with AsyncClient(
        transport=ASGITransport(app=application(service)),
        base_url="https://test",
        cookies={PARTICIPANT_SESSION_COOKIE_NAME: "fixture-one"},
    ) as client:
        response = await client.get("/api/v1/participant/result")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "audience_service_unavailable"
    assert "fixture_deferred" not in response.text
