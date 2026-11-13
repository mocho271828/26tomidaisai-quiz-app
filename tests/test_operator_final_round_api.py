from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest
from httpx import AsyncClient
from test_operator_control_api import (
    CSRF_FIXTURE,
    EVENT_ID,
    IDEMPOTENCY_KEY,
    ROUND_ID,
    VALID_SESSION_FIXTURE,
    RecordingRealtimePublisher,
    StubAuthService,
)
from test_operator_control_api import operator_control_client as operator_control_client

from quiz_app.config import OPERATOR_CSRF_COOKIE_NAME, OPERATOR_SESSION_COOKIE_NAME
from quiz_app.main import app
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import OperatorPrincipal, get_operator_auth_service
from quiz_app.operator.control_contracts import OperatorMutationResponse
from quiz_app.operator.final_round import get_final_round_service
from quiz_app.operator.final_round_contracts import FinalRoundMutationRequest, FinalRoundResponse
from quiz_app.operator.mutations import get_operator_mutation_service
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.realtime.redis_adapter import get_realtime_publisher

PATH = "/api/v1/operator/final-round"


class StubFinalRoundService:
    def get_final_round(self, event_id: UUID) -> FinalRoundResponse:
        assert event_id == EVENT_ID
        return FinalRoundResponse(event_id=event_id, event_version=3, final_round_id=ROUND_ID)


class StubFinalRoundMutationService:
    def __init__(self) -> None:
        self.calls: list[FinalRoundMutationRequest] = []

    def set_final_round(
        self, principal: OperatorPrincipal, event_id: UUID, request: FinalRoundMutationRequest
    ) -> OperatorMutationResponse:
        assert principal.role is AdminRole.OPERATOR
        assert event_id == EVENT_ID
        self.calls.append(request)
        return OperatorMutationResponse(
            operation="set_final_round",
            event_id=event_id,
            event_version=4,
            result_visibility=ResultVisibility(
                distribution=False, personal_ranking=False, overall_ranking=False
            ),
        )


def _request() -> dict[str, object]:
    return {
        "expected_version": 3,
        "idempotency_key": str(IDEMPOTENCY_KEY),
        "round_id": str(ROUND_ID),
        "is_final": True,
    }


def _authenticate(client: AsyncClient) -> None:
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, VALID_SESSION_FIXTURE, path="/api/v1/operator")
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")


@pytest.mark.asyncio
async def test_final_round_get_requires_operator_authentication_and_no_store(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    app.dependency_overrides[get_final_round_service] = StubFinalRoundService
    assert (await client.get(PATH)).status_code == 401
    _authenticate(client)
    response = await client.get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "event_id": str(EVENT_ID),
        "event_version": 3,
        "final_round_id": str(ROUND_ID),
    }


@pytest.mark.asyncio
async def test_final_round_post_requires_csrf_and_notifies_existing_audiences(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    service = StubFinalRoundMutationService()
    publisher = RecordingRealtimePublisher()
    app.dependency_overrides[get_operator_mutation_service] = lambda: service
    app.dependency_overrides[get_realtime_publisher] = lambda: publisher
    assert (await client.post(PATH, json=_request())).status_code == 401
    _authenticate(client)
    assert (await client.post(PATH, json=_request())).status_code == 403
    assert service.calls == [] and publisher.notifications == []
    response = await client.post(PATH, json=_request(), headers={"X-CSRF-Token": CSRF_FIXTURE})
    assert response.status_code == 200
    assert response.json()["operation"] == "set_final_round"
    assert response.json()["event_version"] == 4
    assert service.calls == [FinalRoundMutationRequest.model_validate(_request())]
    assert publisher.notifications == [(EVENT_ID, ("participant", "operator"))]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"reason": " "}, {"is_final": "true"}, {"round_id": "invalid"}, {"extra": 1}]
)
async def test_final_round_post_rejects_invalid_envelope_before_mutation(
    operator_control_client: AsyncClient, changes: dict[str, object]
) -> None:
    client = operator_control_client
    service = StubFinalRoundMutationService()
    app.dependency_overrides[get_operator_mutation_service] = lambda: service
    _authenticate(client)
    response = await client.post(
        PATH, json=_request() | changes, headers={"X-CSRF-Token": CSRF_FIXTURE}
    )
    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_final_round_post_keeps_existing_role_guard(
    operator_control_client: AsyncClient,
) -> None:
    class UnprivilegedAuthService(StubAuthService):
        def authenticate(self, session_token: str) -> OperatorPrincipal:
            # 不正なprincipalが下流へ渡った場合も、既存mutation境界で拒否する。
            return replace(
                super().authenticate(session_token), role=cast(AdminRole, "Unprivileged")
            )

    app.dependency_overrides[get_operator_auth_service] = UnprivilegedAuthService
    client = operator_control_client
    _authenticate(client)
    response = await client.post(PATH, json=_request(), headers={"X-CSRF-Token": CSRF_FIXTURE})
    assert response.status_code == 403


def test_final_round_openapi_preserves_security_and_required_nullable_contract() -> None:
    schema = app.openapi()
    route = schema["paths"][PATH]
    assert set(route) == {"get", "post"}
    assert route["get"]["security"] == route["post"]["security"] == [{"OperatorSessionCookie": []}]
    parameters = {parameter["name"] for parameter in route["post"]["parameters"]}
    assert {OPERATOR_CSRF_COOKIE_NAME, "X-CSRF-Token"} <= parameters
    response = schema["components"]["schemas"]["FinalRoundResponse"]
    assert set(response["required"]) == {"event_id", "event_version", "final_round_id"}
    assert {"type": "null"} in response["properties"]["final_round_id"]["anyOf"]
    request = schema["components"]["schemas"]["FinalRoundMutationRequest"]
    assert {"round_id", "is_final", "expected_version", "idempotency_key"} <= set(
        request["required"]
    )
    assert request["additionalProperties"] is False
