from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from quiz_app.config import OPERATOR_CSRF_COOKIE_NAME, OPERATOR_SESSION_COOKIE_NAME
from quiz_app.domain.errors import RoundBackupFailedError
from quiz_app.main import app
from quiz_app.models.enums import AdminRole, EventState
from quiz_app.operator.auth_service import OperatorPrincipal, get_operator_auth_service
from quiz_app.operator.control_contracts import (
    NextRoundMutationRequest,
    OperatorLatestRankingResponse,
    OperatorMutationRequest,
    OperatorMutationResponse,
    OperatorOptionResponse,
    OperatorQuestionPreviewResponse,
    OperatorRankingRowResponse,
    OperatorStateResponse,
    QuestionSelectionMutationRequest,
    ResultVisibilityMutationRequest,
    ScheduleRoundMutationRequest,
)
from quiz_app.operator.errors import CsrfValidationError, OperatorSessionInvalidError
from quiz_app.operator.mutations import get_operator_mutation_service
from quiz_app.operator.projection import get_operator_projection_service
from quiz_app.operator.response_time_contracts import (
    OperatorResponseTimeFilters,
    OperatorResponseTimeRound,
    OperatorResponseTimesResponse,
)
from quiz_app.operator.response_times import (
    ResponseTimeAnswer,
    get_operator_response_time_service,
    response_time_statistics,
)
from quiz_app.quiz_configuration import ResultVisibility, get_quiz_settings
from quiz_app.realtime.contracts import RealtimeAudience
from quiz_app.realtime.redis_adapter import get_realtime_publisher
from quiz_app.single_event import get_single_event_service

ACCOUNT_ID = UUID("20000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("20000000-0000-0000-0000-000000000002")
ROUND_ID = UUID("20000000-0000-0000-0000-000000000003")
EVENT_ID = UUID("20000000-0000-0000-0000-000000000004")
IDEMPOTENCY_KEY = UUID("20000000-0000-0000-0000-000000000005")
QUESTION_ID = UUID("20000000-0000-0000-0000-000000000006")
OPTION_A_ID = UUID("20000000-0000-0000-0000-000000000007")
OPTION_B_ID = UUID("20000000-0000-0000-0000-000000000008")
VALID_SESSION_FIXTURE = "control-session-value"
CSRF_FIXTURE = "control-csrf-value"
PROJECTED_AT = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)


def _principal() -> OperatorPrincipal:
    return OperatorPrincipal(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        username="demo-operator",
        role=AdminRole.OPERATOR,
        csrf_hash="stored-csrf-hash",
    )


class StubAuthService:
    def authenticate(self, session_token: str) -> OperatorPrincipal:
        if session_token != VALID_SESSION_FIXTURE:
            raise OperatorSessionInvalidError("invalid")
        return _principal()

    def verify_csrf(
        self,
        _: OperatorPrincipal,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> None:
        if csrf_cookie != CSRF_FIXTURE or csrf_header != CSRF_FIXTURE:
            raise CsrfValidationError("invalid")


class StubMutationService:
    def begin_preparation(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
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

    def select_round_question(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: QuestionSelectionMutationRequest,
    ) -> OperatorMutationResponse:
        return OperatorMutationResponse(
            operation="select_round_question",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=round_id,
            round_version=request.expected_version + 1,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
        )

    def schedule_round(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: ScheduleRoundMutationRequest,
    ) -> OperatorMutationResponse:
        return OperatorMutationResponse(
            operation="schedule_round",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=round_id,
            round_version=request.expected_version + 1,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
            open_at=request.target_open_at,
        )

    def publish_round_results(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        return OperatorMutationResponse(
            operation="publish_round_results",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=round_id,
            round_version=request.expected_version + 1,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
        )

    def retry_round_results(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        return OperatorMutationResponse(
            operation="retry_round_results",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=round_id,
            round_version=request.expected_version + 1,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
        )

    def advance_round(
        self,
        _: OperatorPrincipal,
        __: UUID,
        request: NextRoundMutationRequest,
    ) -> OperatorMutationResponse:
        return OperatorMutationResponse(
            operation="advance_round",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=ROUND_ID,
            round_version=request.expected_version + 2,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
        )

    def complete_event(
        self,
        _: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        return OperatorMutationResponse(
            operation="complete_event",
            event_id=EVENT_ID,
            event_version=request.expected_version + 1,
            round_id=round_id,
            round_version=request.expected_version + 1,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
        )

    def set_result_visibility(
        self,
        _: OperatorPrincipal,
        event_id: UUID,
        request: ResultVisibilityMutationRequest,
    ) -> OperatorMutationResponse:
        visibility = ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        )
        targets = (
            ("personal_ranking", "overall_ranking")
            if request.target == "rankings"
            else (request.target,)
        )
        return OperatorMutationResponse(
            operation="set_result_visibility",
            event_id=event_id,
            event_version=request.expected_version + 1,
            result_visibility=visibility.model_copy(
                update={target: request.visible for target in targets}
            ),
        )


class StubProjectionService:
    def get_event_state(self, event_id: UUID) -> OperatorStateResponse:
        return OperatorStateResponse(
            award_ceremony_phase=None,
            event_id=event_id,
            projected_at=PROJECTED_AT,
            event_title="[DEMO] 初回確認",
            event_state=EventState.LOBBY_OPEN,
            event_version=3,
            result_visibility=ResultVisibility(
                distribution=True, personal_ranking=False, overall_ranking=False
            ),
            start_delay_seconds=3,
            visibility_controls=get_quiz_settings().visibility,
            participant_count=2,
            available_questions=[
                OperatorQuestionPreviewResponse(
                    question_id=QUESTION_ID,
                    version=2,
                    display_order=2,
                    prompt="[DEMO] 次の問題",
                    options=[
                        OperatorOptionResponse(
                            option_id=OPTION_A_ID,
                            label="A",
                            content="不正解の選択肢",
                            is_correct=False,
                        ),
                        OperatorOptionResponse(
                            option_id=OPTION_B_ID,
                            label="B",
                            content="正解の選択肢",
                            is_correct=True,
                        ),
                    ],
                )
            ],
            used_question_ids=[],
            current_round=None,
            latest_ranking_snapshot_version=4,
        )

    def get_latest_ranking(
        self,
        event_id: UUID,
    ) -> OperatorLatestRankingResponse | None:
        assert event_id == EVENT_ID
        return OperatorLatestRankingResponse(
            snapshot_version=4,
            round_id=ROUND_ID,
            rows=[
                OperatorRankingRowResponse(
                    overall_rank=1,
                    public_label="AAA",
                    total_points=20_000,
                    correct_count=2,
                ),
                OperatorRankingRowResponse(
                    overall_rank=1,
                    public_label="AAB",
                    total_points=20_000,
                    correct_count=2,
                ),
            ],
        )


class StubRealtimePublisher:
    def notify(self, _: UUID, __: tuple[RealtimeAudience, ...], *, cause: str) -> bool:
        return True


class StubSingleEventService:
    def get_event_id(self) -> UUID:
        return EVENT_ID

    def require_round(self, round_id: UUID) -> UUID:
        assert round_id == ROUND_ID
        return EVENT_ID


class FailingProgressionMutationService(StubMutationService):
    def advance_round(
        self,
        _: OperatorPrincipal,
        __: UUID,
        ___: NextRoundMutationRequest,
    ) -> OperatorMutationResponse:
        raise RoundBackupFailedError("round_backup_verification_failed", event_id=EVENT_ID)

    def complete_event(
        self,
        _: OperatorPrincipal,
        __: UUID,
        ___: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        raise RoundBackupFailedError("round_backup_verification_failed", event_id=EVENT_ID)


class RecordingRealtimePublisher:
    def __init__(self) -> None:
        self.notifications: list[tuple[UUID, tuple[RealtimeAudience, ...]]] = []

    def notify(
        self, event_id: UUID, audiences: tuple[RealtimeAudience, ...], *, cause: str
    ) -> bool:
        self.notifications.append((event_id, audiences))
        return True


class StubResponseTimeService:
    def get_response_times(
        self, event_id: UUID, filters: OperatorResponseTimeFilters
    ) -> OperatorResponseTimesResponse:
        assert event_id == EVENT_ID
        if filters.round_id is not None:
            assert filters.round_id == ROUND_ID
        statistics = response_time_statistics(
            [
                ResponseTimeAnswer(
                    public_label="AAA",
                    response_ms=1_001,
                    is_correct=True,
                )
            ]
        )
        return OperatorResponseTimesResponse(
            timing_basis="server_received_v1",
            rounds=[
                OperatorResponseTimeRound(
                    round_id=ROUND_ID, round_number=1, is_practice=True, prompt="練習問題"
                )
            ],
            selected_round_id=ROUND_ID,
            all_answers=statistics,
            correct_answers=statistics,
        )


@pytest_asyncio.fixture
async def operator_control_client() -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_operator_auth_service] = StubAuthService
    app.dependency_overrides[get_operator_mutation_service] = StubMutationService
    app.dependency_overrides[get_operator_projection_service] = StubProjectionService
    app.dependency_overrides[get_operator_response_time_service] = StubResponseTimeService
    app.dependency_overrides[get_realtime_publisher] = StubRealtimePublisher
    app.dependency_overrides[get_single_event_service] = StubSingleEventService
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_response_times_require_session_and_return_filtered_no_store_response(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    path = "/api/v1/operator/response-times"
    unauthorized = await client.get(path)
    assert unauthorized.status_code == 401

    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, VALID_SESSION_FIXTURE, path="/api/v1/operator")
    default = await client.get(path)
    assert default.status_code == 200
    assert default.headers["Cache-Control"] == "no-store"
    assert "answer_mode" not in default.json()

    response = await client.get(path, params={"round_id": str(ROUND_ID)})
    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_round_id"] == str(ROUND_ID)
    assert "answer_mode" not in payload
    assert payload["all_answers"]["rows"] == [
        {
            "rank": 1,
            "public_label": "AAA",
            "response_ms": 1_001,
            "is_correct": True,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"round_id": "invalid"}])
async def test_response_times_reject_invalid_filters(
    operator_control_client: AsyncClient, params: dict[str, str]
) -> None:
    client = operator_control_client
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, VALID_SESSION_FIXTURE, path="/api/v1/operator")
    response = await client.get("/api/v1/operator/response-times", params=params)
    assert response.status_code == 422


def test_response_times_openapi_has_authenticated_route_and_required_nullable_statistics() -> None:
    schema = app.openapi()
    route = schema["paths"]["/api/v1/operator/response-times"]["get"]
    assert route["security"] == [{"OperatorSessionCookie": []}]
    parameters = {parameter["name"]: parameter for parameter in route["parameters"]}
    assert "answer_mode" not in parameters
    statistics = schema["components"]["schemas"]["OperatorResponseTimeStatistics"]
    for metric in (
        "average_response_ms",
        "median_response_ms",
        "fastest_response_ms",
        "slowest_response_ms",
    ):
        assert metric in statistics["required"]
        assert {"type": "null"} in statistics["properties"][metric]["anyOf"]


@pytest.mark.asyncio
async def test_mutation_requires_matching_csrf_cookie_and_header(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    request = {
        "expected_version": 1,
        "idempotency_key": str(IDEMPOTENCY_KEY),
    }

    forbidden = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/prepare",
        json=request,
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")
    accepted = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/prepare",
        json=request,
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert forbidden.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["operation"] == "begin_preparation"


def test_operator_mutation_openapi_omits_removed_reason_field() -> None:
    schemas = app.openapi()["components"]["schemas"]
    mutation_models = [
        "OperatorMutationRequest",
        "ScheduleRoundMutationRequest",
        "QuestionSelectionMutationRequest",
        "NextRoundMutationRequest",
        "ResultVisibilityMutationRequest",
        "FinalRoundMutationRequest",
        "AwardCeremonyMutationRequest",
    ]
    for model_name in mutation_models:
        model = schemas[model_name]
        assert "reason" not in model["properties"]
        assert "reason" not in model["required"]
        assert {"expected_version", "idempotency_key"} <= set(model["required"])
        assert model["additionalProperties"] is False


@pytest.mark.asyncio
async def test_operator_projection_route_requires_session_and_returns_state(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client

    unauthorized = await client.get("/api/v1/operator/state")
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    accepted = await client.get("/api/v1/operator/state")

    assert unauthorized.status_code == 401
    assert "[DEMO] 次の問題" not in unauthorized.text
    assert "正解の選択肢" not in unauthorized.text
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["event_version"] == 3
    assert accepted.json()["projected_at"] == "2026-08-08T10:00:00Z"
    assert accepted.json()["result_visibility"] == {
        "distribution": True,
        "personal_ranking": False,
        "overall_ranking": False,
    }
    assert accepted.json()["latest_ranking_snapshot_version"] == 4
    assert accepted.json()["available_questions"] == [
        {
            "question_id": str(QUESTION_ID),
            "version": 2,
            "display_order": 2,
            "prompt": "[DEMO] 次の問題",
            "options": [
                {
                    "option_id": str(OPTION_A_ID),
                    "label": "A",
                    "content": "不正解の選択肢",
                    "is_correct": False,
                },
                {
                    "option_id": str(OPTION_B_ID),
                    "label": "B",
                    "content": "正解の選択肢",
                    "is_correct": True,
                },
            ],
        }
    ]
    assert accepted.json()["used_question_ids"] == []


@pytest.mark.asyncio
async def test_latest_ranking_route_requires_session_and_returns_no_store_response(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client

    unauthorized = await client.get("/api/v1/operator/ranking")
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    accepted = await client.get("/api/v1/operator/ranking")

    assert unauthorized.status_code == 401
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json() == {
        "snapshot_version": 4,
        "round_id": str(ROUND_ID),
        "rows": [
            {
                "overall_rank": 1,
                "public_label": "AAA",
                "total_points": 20_000,
                "correct_count": 2,
            },
            {
                "overall_rank": 1,
                "public_label": "AAB",
                "total_points": 20_000,
                "correct_count": 2,
            },
        ],
    }


@pytest.mark.asyncio
async def test_question_selection_requires_csrf_and_uses_typed_request(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")

    response = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/question",
        json={
            "expected_version": 1,
            "idempotency_key": str(IDEMPOTENCY_KEY),
            "question_id": str(QUESTION_ID),
        },
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert response.status_code == 200
    assert response.json()["operation"] == "select_round_question"
    assert response.json()["round_version"] == 2


@pytest.mark.asyncio
async def test_schedule_route_accepts_aware_deadline_and_exact_target(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")
    target_open_at = "2026-08-08T10:00:05Z"

    response = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/schedule",
        json={
            "expected_version": 1,
            "idempotency_key": str(IDEMPOTENCY_KEY),
            "execute_not_after": "2026-08-08T10:00:06Z",
            "target_open_at": target_open_at,
        },
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert response.status_code == 200
    assert response.json()["open_at"] == target_open_at


@pytest.mark.asyncio
async def test_schedule_route_rejects_naive_target_datetime(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")

    response = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/schedule",
        json={
            "expected_version": 1,
            "idempotency_key": str(IDEMPOTENCY_KEY),
            "execute_not_after": "2026-08-08T10:00:06Z",
            "target_open_at": "2026-08-08T10:00:05",
        },
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_retry_results_requires_csrf_and_uses_typed_request(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    request = {
        "expected_version": 3,
        "idempotency_key": str(IDEMPOTENCY_KEY),
    }

    forbidden = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/retry-results",
        json=request,
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")
    accepted = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/retry-results",
        json=request,
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert forbidden.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["operation"] == "retry_round_results"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "request_body", "expected_operation"),
    [
        (
            "advance",
            {"question_id": str(QUESTION_ID)},
            "advance_round",
        ),
        ("complete-event", {}, "complete_event"),
    ],
)
async def test_round_progression_requires_csrf_and_uses_typed_request(
    operator_control_client: AsyncClient,
    path: str,
    request_body: dict[str, str],
    expected_operation: str,
) -> None:
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    payload = {
        "expected_version": 3,
        "idempotency_key": str(IDEMPOTENCY_KEY),
        **request_body,
    }

    forbidden = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/{path}",
        json=payload,
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")
    accepted = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/{path}",
        json=payload,
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert forbidden.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["operation"] == expected_operation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "request_body"),
    [
        ("advance", {"question_id": str(QUESTION_ID)}),
        ("complete-event", {}),
    ],
)
async def test_round_backup_failure_notifies_all_audiences_after_commit(
    operator_control_client: AsyncClient,
    path: str,
    request_body: dict[str, str],
) -> None:
    publisher = RecordingRealtimePublisher()
    app.dependency_overrides[get_operator_mutation_service] = FailingProgressionMutationService
    app.dependency_overrides[get_realtime_publisher] = lambda: publisher
    client = operator_control_client
    client.cookies.set(
        OPERATOR_SESSION_COOKIE_NAME,
        VALID_SESSION_FIXTURE,
        path="/api/v1/operator",
    )
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")

    response = await client.post(
        f"/api/v1/operator/rounds/{ROUND_ID}/{path}",
        json={
            "expected_version": 3,
            "idempotency_key": str(IDEMPOTENCY_KEY),
            **request_body,
        },
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "round_backup_failed"
    assert publisher.notifications == [
        (EVENT_ID, ("participant", "operator")),
    ]


def test_openapi_describes_session_as_security_and_csrf_as_parameters() -> None:
    schema = app.openapi()
    for path in (
        "/api/v1/operator/rounds/{round_id}/prepare",
        "/api/v1/operator/rounds/{round_id}/question",
        "/api/v1/operator/rounds/{round_id}/retry-results",
        "/api/v1/operator/rounds/{round_id}/advance",
        "/api/v1/operator/rounds/{round_id}/complete-event",
    ):
        operation = schema["paths"][path]["post"]
        assert operation["security"] == [{"OperatorSessionCookie": []}]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert {OPERATOR_CSRF_COOKIE_NAME, "X-CSRF-Token"} <= parameters.keys()
        assert all(
            "403" in parameters[name]["description"]
            for name in {OPERATOR_CSRF_COOKIE_NAME, "X-CSRF-Token"}
        )
    paths = schema["paths"]
    for path in (
        "/api/v1/operator/state",
        "/api/v1/operator/ranking",
    ):
        assert paths[path]["get"]["security"] == [{"OperatorSessionCookie": []}]
    assert {
        "/api/v1/operator/state",
        "/api/v1/operator/ranking",
        "/api/v1/operator/rounds/{round_id}/prepare",
        "/api/v1/operator/rounds/{round_id}/question",
        "/api/v1/operator/rounds/{round_id}/schedule",
        "/api/v1/operator/rounds/{round_id}/retry-results",
        "/api/v1/operator/rounds/{round_id}/advance",
        "/api/v1/operator/rounds/{round_id}/complete-event",
        "/api/v1/operator/result-visibility",
    } <= paths.keys()
    assert "/api/v1/operator/rounds/{round_id}/publish-results" in paths
    assert "/api/v1/operator/events/{event_id}" not in paths
    assert "/api/v1/operator/events/{event_id}/ranking" not in paths
    assert "/api/v1/operator/events/{event_id}/ranking-visibility" not in paths


def test_openapi_requires_nullable_operator_statistics_fields() -> None:
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    round_statistics = schemas["OperatorRoundStatisticsResponse"]
    option_statistics = schemas["OperatorOptionStatisticsResponse"]
    operator_state = schemas["OperatorStateResponse"]
    question_preview = schemas["OperatorQuestionPreviewResponse"]
    operator_option = schemas["OperatorOptionResponse"]
    latest_ranking = schemas["OperatorLatestRankingResponse"]
    ranking_row = schemas["OperatorRankingRowResponse"]

    assert "average_response_ms" in round_statistics["required"]
    assert "average_response_ms" in option_statistics["required"]
    assert "latest_ranking_snapshot_version" in operator_state["required"]
    assert set(question_preview["required"]) == {
        "question_id",
        "version",
        "prompt",
        "options",
        "display_order",
    }
    assert "is_correct" in operator_option["required"]
    assert set(latest_ranking["required"]) == {"snapshot_version", "round_id", "rows"}
    assert set(ranking_row["required"]) == {
        "overall_rank",
        "public_label",
        "total_points",
        "correct_count",
    }
    response_schema = schema["paths"]["/api/v1/operator/ranking"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert {"$ref": "#/components/schemas/OperatorLatestRankingResponse"} in response_schema[
        "anyOf"
    ]
    assert {"type": "null"} in response_schema["anyOf"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["distribution", "personal_ranking", "overall_ranking", "rankings"]
)
async def test_result_visibility_requires_session_csrf_and_typed_target(
    operator_control_client: AsyncClient,
    target: str,
) -> None:
    client = operator_control_client
    publisher = RecordingRealtimePublisher()
    app.dependency_overrides[get_realtime_publisher] = lambda: publisher
    request = {
        "expected_version": 1,
        "idempotency_key": str(IDEMPOTENCY_KEY),
        "target": target,
        "visible": True,
    }
    unauthorized = await client.post("/api/v1/operator/result-visibility", json=request)
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, VALID_SESSION_FIXTURE, path="/api/v1/operator")
    forbidden = await client.post("/api/v1/operator/result-visibility", json=request)
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")
    accepted = await client.post(
        "/api/v1/operator/result-visibility",
        json=request,
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )
    invalid_type = await client.post(
        "/api/v1/operator/result-visibility",
        json={**request, "visible": "false"},
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )
    invalid_target = await client.post(
        "/api/v1/operator/result-visibility",
        json={**request, "target": "ranking_visibility"},
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )
    removed_route = await client.post(
        "/api/v1/operator/ranking-visibility",
        json=request,
        headers={"X-CSRF-Token": CSRF_FIXTURE},
    )

    assert unauthorized.status_code == 401
    assert forbidden.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["operation"] == "set_result_visibility"
    if target == "rankings":
        assert accepted.json()["result_visibility"] == {
            "distribution": True,
            "personal_ranking": True,
            "overall_ranking": True,
        }
    else:
        assert accepted.json()["result_visibility"][target] is True
    assert publisher.notifications == [(EVENT_ID, ("participant", "operator"))]
    assert invalid_type.status_code == 422
    assert invalid_target.status_code == 422
    assert removed_route.status_code == 404


def test_openapi_accepts_grouped_rankings_without_changing_visibility_fields() -> None:
    schemas = app.openapi()["components"]["schemas"]
    target = schemas["ResultVisibilityMutationRequest"]["properties"]["target"]
    assert {"const": "rankings", "type": "string"} in target["anyOf"]
    assert set(schemas["ResultVisibility"]["properties"]) == {
        "distribution",
        "personal_ranking",
        "overall_ranking",
    }


@pytest.mark.asyncio
async def test_manual_publish_requires_session_and_csrf_then_notifies_both_audiences(
    operator_control_client: AsyncClient,
) -> None:
    client = operator_control_client
    publisher = RecordingRealtimePublisher()
    app.dependency_overrides[get_realtime_publisher] = lambda: publisher
    path = f"/api/v1/operator/rounds/{ROUND_ID}/publish-results"
    request = {
        "expected_version": 5,
        "idempotency_key": str(IDEMPOTENCY_KEY),
    }
    assert (await client.post(path, json=request)).status_code == 401
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, VALID_SESSION_FIXTURE, path="/api/v1/operator")
    assert (await client.post(path, json=request)).status_code == 403
    client.cookies.set(OPERATOR_CSRF_COOKIE_NAME, CSRF_FIXTURE, path="/")
    response = await client.post(path, json=request, headers={"X-CSRF-Token": CSRF_FIXTURE})
    assert response.status_code == 200
    assert response.json()["operation"] == "publish_round_results"
    assert publisher.notifications == [(EVENT_ID, ("participant", "operator"))]
