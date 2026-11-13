from uuid import UUID

import pytest
from httpx import AsyncClient
from test_operator_control_api import CSRF_FIXTURE, EVENT_ID, RecordingRealtimePublisher
from test_operator_control_api import operator_control_client as operator_control_client
from test_operator_final_round_api import _authenticate, _request

from quiz_app.main import app
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import (
    AwardCeremonyMutationRequest,
    OperatorMutationResponse,
)
from quiz_app.operator.mutations import get_operator_mutation_service
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.redis_adapter import get_realtime_publisher

PATH = "/api/v1/operator/award-ceremony"


class StubAwardCeremonyMutationService:
    def __init__(self) -> None:
        self.calls: list[AwardCeremonyMutationRequest] = []

    def set_award_ceremony(
        self, principal: OperatorPrincipal, event_id: UUID, request: AwardCeremonyMutationRequest
    ) -> OperatorMutationResponse:
        assert principal.username == "demo-operator"
        assert event_id == EVENT_ID
        self.calls.append(request)
        return OperatorMutationResponse(
            operation="set_award_ceremony",
            event_id=event_id,
            event_version=4,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
        )


def request_payload() -> dict[str, object]:
    return {
        key: value for key, value in _request().items() if key not in {"round_id", "is_final"}
    } | {"phase": "Announced"}


@pytest.mark.asyncio
async def test_auth_csrf_and_post_commit_notification(operator_control_client: AsyncClient) -> None:
    client = operator_control_client
    service = StubAwardCeremonyMutationService()
    publisher = RecordingRealtimePublisher()
    app.dependency_overrides[get_operator_mutation_service] = lambda: service
    app.dependency_overrides[get_realtime_publisher] = lambda: publisher
    assert (await client.post(PATH, json=request_payload())).status_code == 401
    _authenticate(client)
    assert (await client.post(PATH, json=request_payload())).status_code == 403
    assert service.calls == [] and publisher.notifications == []
    response = await client.post(
        PATH, json=request_payload(), headers={"X-CSRF-Token": CSRF_FIXTURE}
    )
    assert response.status_code == 200
    assert response.json()["operation"] == "set_award_ceremony"
    assert service.calls == [AwardCeremonyMutationRequest.model_validate(request_payload())]
    assert publisher.notifications == [(EVENT_ID, ("participant", "operator"))]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"phase": "Waiting"}, {"phase": True}, {"reason": " "}, {"extra": 1}]
)
async def test_invalid_requests_never_mutate(
    operator_control_client: AsyncClient, changes: dict[str, object]
) -> None:
    client = operator_control_client
    service = StubAwardCeremonyMutationService()
    app.dependency_overrides[get_operator_mutation_service] = lambda: service
    _authenticate(client)
    response = await client.post(
        PATH, json=request_payload() | changes, headers={"X-CSRF-Token": CSRF_FIXTURE}
    )
    assert response.status_code == 422
    assert service.calls == []


def test_openapi_exposes_required_nullable_projection_fields_and_operator_only_mutation() -> None:
    schema = app.openapi()
    assert set(schema["paths"][PATH]) == {"post"}
    assert schema["paths"][PATH]["post"]["security"] == [{"OperatorSessionCookie": []}]
    models = schema["components"]["schemas"]
    assert models["AwardCeremonyMutationRequest"]["additionalProperties"] is False
    assert models["AwardCeremonyMutationRequest"]["properties"]["phase"]["enum"] == [
        "Announced",
        "Finished",
    ]
    for name in ("ParticipantCommonStateResponse", "ParticipantStateResponse"):
        assert "award_ceremony" in models[name]["required"]
        assert {"type": "null"} in models[name]["properties"]["award_ceremony"]["anyOf"]
    assert "award_ceremony_phase" in models["OperatorStateResponse"]["required"]
    assert set(models["AwardCeremonyRankingRowResponse"]["properties"]) == {
        "public_label",
        "overall_rank",
    }
    assert models["AwardCeremonyRankingRowResponse"]["properties"]["overall_rank"]["maximum"] == 5
