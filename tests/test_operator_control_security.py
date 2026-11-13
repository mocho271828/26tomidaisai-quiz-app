from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from quiz_app.models.audit import IdempotencyRecord
from quiz_app.operator.control_contracts import (
    OperatorMutationRequest,
    OperatorMutationResponse,
    QuestionSelectionMutationRequest,
    ScheduleRoundMutationRequest,
)
from quiz_app.operator.errors import IdempotencyKeyConflictError
from quiz_app.operator.mutations import (
    OperatorMutationService,
    advisory_lock_key,
    mutation_request_hash,
    mutation_resulting_version,
)
from quiz_app.quiz_configuration import ResultVisibility

ACCOUNT_ID = UUID("30000000-0000-0000-0000-000000000001")
ROUND_ID = UUID("30000000-0000-0000-0000-000000000002")
OTHER_ROUND_ID = UUID("30000000-0000-0000-0000-000000000003")
IDEMPOTENCY_KEY = UUID("30000000-0000-0000-0000-000000000004")
QUESTION_ID = UUID("30000000-0000-0000-0000-000000000005")
OTHER_QUESTION_ID = UUID("30000000-0000-0000-0000-000000000006")


@pytest.mark.parametrize("reason", ["クイズ進行", "", " "])
def test_mutation_rejects_removed_reason_field(reason: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OperatorMutationRequest(
            expected_version=1,
            idempotency_key=IDEMPOTENCY_KEY,
            reason=reason,
        )


def test_mutation_requires_no_reason_and_omits_it_from_serialized_request() -> None:
    request = OperatorMutationRequest(
        expected_version=1,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    assert "reason" not in request.model_dump()


def test_mutation_deadlines_require_timezone_aware_datetimes() -> None:
    with pytest.raises(ValidationError, match="timezone_aware"):
        OperatorMutationRequest(
            expected_version=1,
            idempotency_key=IDEMPOTENCY_KEY,
            # aware datetime契約の拒否を確認するため、意図的にnaive値を渡す。
            execute_not_after=datetime(2026, 8, 9),  # noqa: DTZ001
        )

    with pytest.raises(ValidationError, match="timezone_aware"):
        ScheduleRoundMutationRequest(
            expected_version=1,
            idempotency_key=IDEMPOTENCY_KEY,
            target_open_at=datetime(2026, 8, 9),  # noqa: DTZ001
        )


def test_mutation_hash_includes_operation_target_and_body() -> None:
    request = OperatorMutationRequest(
        expected_version=1,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    first = mutation_request_hash("begin_preparation", ROUND_ID, request)

    assert first == mutation_request_hash("begin_preparation", ROUND_ID, request)
    assert first != mutation_request_hash("schedule_round", ROUND_ID, request)
    assert first != mutation_request_hash("begin_preparation", OTHER_ROUND_ID, request)
    deadline_request = request.model_copy(
        update={"execute_not_after": datetime(2026, 8, 9, tzinfo=UTC)}
    )
    assert first != mutation_request_hash("begin_preparation", ROUND_ID, deadline_request)

    selection = QuestionSelectionMutationRequest(
        expected_version=1,
        idempotency_key=IDEMPOTENCY_KEY,
        question_id=QUESTION_ID,
    )
    other_selection = selection.model_copy(update={"question_id": OTHER_QUESTION_ID})
    assert mutation_request_hash("select_round_question", ROUND_ID, selection) != (
        mutation_request_hash("select_round_question", ROUND_ID, other_selection)
    )


def test_advisory_lock_key_is_signed_64_bit_and_scoped_by_account() -> None:
    first = advisory_lock_key(ACCOUNT_ID, IDEMPOTENCY_KEY)
    second = advisory_lock_key(UUID(int=9), IDEMPOTENCY_KEY)

    assert -(2**63) <= first < 2**63
    assert first != second


def test_idempotency_resulting_version_matches_mutation_target() -> None:
    round_response = OperatorMutationResponse(
        operation="begin_preparation",
        event_id=UUID(int=20),
        event_version=9,
        round_id=ROUND_ID,
        round_version=4,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        ),
    )
    event_response = OperatorMutationResponse(
        operation="set_result_visibility",
        event_id=UUID(int=20),
        event_version=10,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=True, overall_ranking=True
        ),
    )
    selection_response = OperatorMutationResponse(
        operation="select_round_question",
        event_id=UUID(int=20),
        event_version=10,
        round_id=ROUND_ID,
        round_version=5,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        ),
    )

    assert mutation_resulting_version("begin_preparation", round_response) == 4
    assert mutation_resulting_version("select_round_question", selection_response) == 5
    assert mutation_resulting_version("set_result_visibility", event_response) == 10


def test_stored_idempotency_response_replays_without_revalidating_version() -> None:
    response = OperatorMutationResponse(
        operation="begin_preparation",
        event_id=UUID(int=20),
        event_version=7,
        round_id=ROUND_ID,
        round_version=7,
        result_visibility=ResultVisibility(
            distribution=True, personal_ranking=False, overall_ranking=False
        ),
    )
    record = IdempotencyRecord(
        account_id=ACCOUNT_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        operation="begin_preparation",
        request_hash="same-request-hash",
        response_status=200,
        response_json=response.model_dump_json(),
        resulting_version=7,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
    )

    replay = OperatorMutationService._replay(
        record,
        "begin_preparation",
        "same-request-hash",
    )

    assert replay.is_replay is True
    assert replay.event_version == 7
    assert replay.round_version == 7


def test_idempotency_key_reuse_with_different_request_is_rejected() -> None:
    record = IdempotencyRecord(
        account_id=ACCOUNT_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        operation="begin_preparation",
        request_hash="original-request-hash",
        response_status=200,
        response_json="{}",
        resulting_version=2,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
    )

    with pytest.raises(IdempotencyKeyConflictError):
        OperatorMutationService._replay(
            record,
            "begin_preparation",
            "different-request-hash",
        )
