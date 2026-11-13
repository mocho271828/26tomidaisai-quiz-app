from uuid import UUID

import pytest
from pydantic import ValidationError

from quiz_app.models.enums import RoundResultPublication, RoundState
from quiz_app.operator.control_contracts import (
    OperatorOptionResponse,
    OperatorOptionStatisticsResponse,
    OperatorQuestionPreviewResponse,
    OperatorQuestionResponse,
    OperatorRoundResponse,
    OperatorRoundStatisticsResponse,
)

OPTION_A_ID = UUID("10000000-0000-4000-8000-000000000001")
OPTION_B_ID = UUID("10000000-0000-4000-8000-000000000002")


@pytest.mark.parametrize(
    ("answer_count", "average_response_ms"),
    [(0, 0.0), (1, None)],
)
def test_option_statistics_require_average_exactly_for_nonzero_count(
    answer_count: int,
    average_response_ms: float | None,
) -> None:
    with pytest.raises(ValidationError, match="null exactly when count is zero"):
        OperatorOptionStatisticsResponse(
            option_id=OPTION_A_ID,
            answer_count=answer_count,
            average_response_ms=average_response_ms,
        )


@pytest.mark.parametrize(
    ("answer_count", "average_response_ms", "option_count", "option_average"),
    [(0, 0.0, 0, None), (1, None, 1, 1_000.0)],
)
def test_round_statistics_require_average_exactly_for_nonzero_total(
    answer_count: int,
    average_response_ms: float | None,
    option_count: int,
    option_average: float | None,
) -> None:
    with pytest.raises(ValidationError, match="null exactly when total count is zero"):
        OperatorRoundStatisticsResponse(
            answer_count=answer_count,
            average_response_ms=average_response_ms,
            options=[
                OperatorOptionStatisticsResponse(
                    option_id=OPTION_A_ID,
                    answer_count=option_count,
                    average_response_ms=option_average,
                )
            ],
        )


def test_round_statistics_require_total_count_to_equal_option_counts() -> None:
    with pytest.raises(ValidationError, match="sum of option answer counts"):
        OperatorRoundStatisticsResponse(
            answer_count=2,
            average_response_ms=1_000,
            options=[
                OperatorOptionStatisticsResponse(
                    option_id=OPTION_A_ID,
                    answer_count=1,
                    average_response_ms=1_000,
                )
            ],
        )


def test_round_statistics_reject_duplicate_option_ids() -> None:
    with pytest.raises(ValidationError, match="statistics option_id must be unique"):
        OperatorRoundStatisticsResponse(
            answer_count=0,
            average_response_ms=None,
            options=[
                OperatorOptionStatisticsResponse(
                    option_id=OPTION_A_ID,
                    answer_count=0,
                    average_response_ms=None,
                ),
                OperatorOptionStatisticsResponse(
                    option_id=OPTION_A_ID,
                    answer_count=0,
                    average_response_ms=None,
                ),
            ],
        )


def test_operator_question_rejects_duplicate_option_ids() -> None:
    option = OperatorOptionResponse(
        option_id=OPTION_A_ID,
        label="A",
        content="選択肢A",
        is_correct=True,
    )

    with pytest.raises(ValidationError, match="question option_id must be unique"):
        OperatorQuestionResponse(
            question_id=UUID(int=3),
            version=1,
            prompt="問題",
            options=[option, option.model_copy()],
        )


def test_operator_question_preview_inherits_full_strict_question_contract() -> None:
    preview = OperatorQuestionPreviewResponse(
        question_id=UUID(int=3),
        version=1,
        display_order=2,
        prompt="次の問題",
        options=[
            OperatorOptionResponse(
                option_id=OPTION_A_ID,
                label="A",
                content="選択肢A",
                is_correct=False,
            ),
            OperatorOptionResponse(
                option_id=OPTION_B_ID,
                label="B",
                content="選択肢B",
                is_correct=True,
            ),
        ],
    )

    assert isinstance(preview, OperatorQuestionResponse)
    assert [option.is_correct for option in preview.options] == [False, True]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OperatorQuestionPreviewResponse.model_validate(
            {
                **preview.model_dump(),
                "explanation": "運営projectionにも未定義fieldは追加できない",
            }
        )
    with pytest.raises(ValidationError, match="question option_id must be unique"):
        OperatorQuestionPreviewResponse.model_validate(
            {
                **preview.model_dump(),
                "options": [preview.options[0], preview.options[0].model_copy()],
            }
        )


def test_operator_question_preview_requires_all_full_question_fields() -> None:
    with pytest.raises(ValidationError, match="options"):
        OperatorQuestionPreviewResponse.model_validate(
            {
                "question_id": UUID(int=3),
                "version": 1,
                "display_order": 2,
                "prompt": "次の問題",
            }
        )


def test_operator_round_requires_matching_question_and_statistics_option_ids() -> None:
    with pytest.raises(ValidationError, match="option_id sets must match"):
        OperatorRoundResponse.model_validate(
            {
                "round_id": UUID(int=1),
                "version": 1,
                "state_version": 1,
                "state": RoundState.OPEN,
                "open_at": None,
                "standard_close_at": None,
                "can_publish_results": False,
                "result_publication": RoundResultPublication.WITHHELD,
                "result_failure_code": None,
                "statistics": {
                    "answer_count": 0,
                    "average_response_ms": None,
                    "options": [
                        {
                            "option_id": OPTION_B_ID,
                            "answer_count": 0,
                            "average_response_ms": None,
                        }
                    ],
                },
                "question": {
                    "question_id": UUID(int=3),
                    "version": 1,
                    "prompt": "問題",
                    "options": [
                        {
                            "option_id": OPTION_A_ID,
                            "label": "A",
                            "content": "選択肢A",
                            "is_correct": True,
                        }
                    ],
                },
            }
        )


@pytest.mark.parametrize(
    ("state", "failure_code"),
    [
        (RoundState.VOID_CANDIDATE, None),
        (RoundState.OPEN, "automatic_aggregate_integrity_failure"),
        (RoundState.VOID_CANDIDATE, "raw database exception"),
    ],
)
def test_operator_result_failure_code_is_sanitized_and_scoped_to_void_candidate(
    state: RoundState,
    failure_code: str | None,
) -> None:
    with pytest.raises(ValidationError):
        OperatorRoundResponse.model_validate(
            {
                "round_id": UUID(int=1),
                "version": 1,
                "state_version": 1,
                "state": state,
                "open_at": None,
                "standard_close_at": None,
                "can_publish_results": False,
                "result_publication": RoundResultPublication.WITHHELD,
                "result_failure_code": failure_code,
                "statistics": {
                    "answer_count": 0,
                    "average_response_ms": None,
                    "options": [
                        {
                            "option_id": OPTION_A_ID,
                            "answer_count": 0,
                            "average_response_ms": None,
                        }
                    ],
                },
                "question": {
                    "question_id": UUID(int=3),
                    "version": 1,
                    "prompt": "問題",
                    "options": [
                        {
                            "option_id": OPTION_A_ID,
                            "label": "A",
                            "content": "選択肢A",
                            "is_correct": True,
                        }
                    ],
                },
            }
        )


def test_void_candidate_is_reserved_for_typed_automatic_result_failure() -> None:
    response = OperatorRoundResponse(
        round_id=UUID(int=1),
        version=2,
        state_version=8,
        state=RoundState.VOID_CANDIDATE,
        open_at=None,
        standard_close_at=None,
        can_publish_results=False,
        result_publication=RoundResultPublication.WITHHELD,
        result_failure_code="automatic_aggregate_integrity_failure",
        statistics=OperatorRoundStatisticsResponse(
            answer_count=0,
            average_response_ms=None,
            options=[
                OperatorOptionStatisticsResponse(
                    option_id=OPTION_A_ID,
                    answer_count=0,
                    average_response_ms=None,
                )
            ],
        ),
        question=OperatorQuestionResponse(
            question_id=UUID(int=3),
            version=1,
            prompt="問題",
            options=[
                OperatorOptionResponse(
                    option_id=OPTION_A_ID,
                    label="A",
                    content="選択肢A",
                    is_correct=True,
                )
            ],
        ),
    )

    assert response.result_failure_code == "automatic_aggregate_integrity_failure"
