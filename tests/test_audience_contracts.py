from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AudienceEventResponse,
    AudienceQuestionResponse,
    ParticipantAnswerResponse,
    ParticipantCurrentRoundResponse,
    ParticipantIdentityResponse,
    ParticipantJoinRequest,
    ParticipantRankingResponse,
    ParticipantStateResponse,
    PersonalResultDelivery,
    PersonalResultPayload,
    PublishedOptionResponse,
    PublishedResultResponse,
    RoundTimelineResponse,
    ScoredParticipantOutcomeResponse,
    SubmitAnswerRequest,
    UnansweredParticipantOutcomeResponse,
    UnsavedAnswerStatusResponse,
)
from quiz_app.models.enums import EventState, RoundState, SpeedCohort
from quiz_app.quiz_configuration import ResultVisibility

EVENT_ID = UUID("40000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("40000000-0000-4000-8000-000000000002")
OPTION_ID = UUID("40000000-0000-4000-8000-000000000003")
CLIENT_ANSWER_ID = UUID("40000000-0000-4000-8000-000000000004")
CLIENT_JOIN_VALUE_FIXTURE = "A" * 43
PROJECTED_AT = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize("invalid_number", [None, 0, -1, 1.5, "4", True])
def test_round_number_is_a_required_positive_integer(invalid_number: object) -> None:
    timeline = RoundTimelineResponse(
        round_id=ROUND_ID,
        round_number=4,
        version=74,
        state_version=297,
        state=RoundState.OPEN,
        open_at=PROJECTED_AT,
        standard_close_at=PROJECTED_AT,
    )
    assert timeline.round_number == 4
    assert timeline.version == 74
    payload = timeline.model_dump()
    payload["round_number"] = invalid_number
    with pytest.raises(ValidationError):
        RoundTimelineResponse.model_validate(payload)
    del payload["round_number"]
    with pytest.raises(ValidationError):
        RoundTimelineResponse.model_validate(payload)


def test_current_round_contracts_reject_published_result_fields() -> None:
    with pytest.raises(ValidationError):
        ParticipantCurrentRoundResponse.model_validate(
            {
                "round_id": ROUND_ID,
                "round_number": 1,
                "version": 2,
                "state_version": 8,
                "state": RoundState.SCHEDULED,
                "open_at": PROJECTED_AT,
                "standard_close_at": PROJECTED_AT,
                "result": {"publication": "Withheld"},
            }
        )


def test_personal_result_payload_requires_strict_scored_or_unanswered_outcome() -> None:
    scored = PersonalResultPayload(
        selected_option_id=OPTION_ID,
        ranking=None,
        outcome=ScoredParticipantOutcomeResponse(
            timing_basis="server_received_v1",
            status="Scored",
            is_correct=True,
            base_points=400,
            speed_points=550,
            elapsed_ms=2450,
            total_points=950,
            scoring_policy_version="scoring-demo-v1",
            speed_cohort=SpeedCohort.CORRECT,
            speed_rank=1,
            speed_cohort_size=4,
        ),
    )
    unanswered = PersonalResultPayload(
        selected_option_id=None,
        ranking=None,
        outcome=UnansweredParticipantOutcomeResponse(status="Unanswered"),
    )
    assert scored.outcome is not None
    assert scored.outcome.status == "Scored"
    assert unanswered.model_dump(mode="json")["outcome"] == {"status": "Unanswered"}
    assert unanswered.selected_option_id is None
    missing_selected_option = unanswered.model_dump(mode="json")
    del missing_selected_option["selected_option_id"]
    with pytest.raises(ValidationError):
        PersonalResultPayload.model_validate(missing_selected_option)
    with pytest.raises(ValidationError):
        PersonalResultPayload.model_validate(
            {
                **scored.model_dump(mode="json"),
                "outcome": {
                    "timing_basis": "server_received_v1",
                    "status": "Scored",
                    "is_correct": True,
                    "base_points": 400,
                    "speed_points": 550,
                    "elapsed_ms": 2450,
                    "total_points": "950",
                    "scoring_policy_version": "scoring-demo-v1",
                    "speed_cohort": "Correct",
                    "speed_rank": 1,
                    "speed_cohort_size": 4,
                },
            }
        )
    with pytest.raises(ValidationError):
        PersonalResultPayload.model_validate(
            {
                **unanswered.model_dump(mode="json"),
                "outcome": {"status": "Unanswered", "total_points": 0},
            }
        )


def test_published_result_rejects_participant_outcome() -> None:
    with pytest.raises(ValidationError):
        PublishedResultResponse.model_validate(
            {
                "publication": "Published",
                "correct_option_id": OPTION_ID,
                "distribution": {"valid_answer_count": 0, "options": []},
                "outcome": {"status": "Unanswered"},
            }
        )


def test_unavailable_optional_fields_are_omitted_from_serialized_state() -> None:
    response = ParticipantStateResponse(
        award_ceremony=None,
        result_visibility=ResultVisibility(
            distribution=False, personal_ranking=False, overall_ranking=False
        ),
        latest_ranking_snapshot_version=None,
        personal_result_delivery=PersonalResultDelivery(status="none", revision=None),
        projected_at=PROJECTED_AT,
        event=AudienceEventResponse(
            timing_basis="server_received_v1",
            arrival_grace_ms=0,
            clock_tolerance_ms=0,
            client_ahead_tolerance_ms=0,
            event_id=EVENT_ID,
            event_title="[DEMO] 初回確認",
            event_state=EventState.LOBBY_OPEN,
            event_version=1,
            start_delay_seconds=3,
        ),
        participant=ParticipantIdentityResponse(
            public_label="ABC",
            is_ranking_eligible=True,
        ),
        current_round=ParticipantCurrentRoundResponse(
            round_id=ROUND_ID,
            round_number=1,
            version=2,
            state_version=8,
            state=RoundState.SCHEDULED,
            open_at=PROJECTED_AT,
            standard_close_at=PROJECTED_AT,
        ),
        latest_result=None,
    )

    payload = response.model_dump(mode="json")

    assert "answer" not in payload
    assert "ranking" not in payload
    assert "question" not in payload["current_round"]
    assert "result" not in payload["current_round"]
    assert payload["latest_result"] is None


@pytest.mark.parametrize(
    "response_model",
    [ParticipantStateResponse],
)
def test_operator_question_preview_is_rejected_by_audience_state_contracts(
    response_model: type[ParticipantStateResponse],
) -> None:
    payload: dict[str, object] = {
        "projected_at": PROJECTED_AT,
        "event": {
            "timing_basis": "server_received_v1",
            "arrival_grace_ms": 0,
            "clock_tolerance_ms": 0,
            "client_ahead_tolerance_ms": 0,
            "event_id": EVENT_ID,
            "event_title": "[DEMO] 初回確認",
            "event_state": EventState.LOBBY_OPEN,
            "event_version": 1,
            "start_delay_seconds": 3,
        },
        "current_round": None,
        "latest_result": None,
        "available_questions": [
            {
                "question_id": UUID(int=90),
                "version": 2,
                "display_order": 2,
                "prompt": "NEXT-QUESTION-ONLY-FOR-OPERATOR",
                "options": [
                    {
                        "option_id": UUID(int=91),
                        "label": "A",
                        "content": "NEXT-OPTION-ONLY-FOR-OPERATOR",
                        "is_correct": True,
                    }
                ],
            }
        ],
    }
    if response_model is ParticipantStateResponse:
        payload["participant"] = {
            "public_label": "ABC",
            "is_ranking_eligible": True,
        }

    with pytest.raises(ValidationError):
        response_model.model_validate(payload)


def test_audience_question_accepts_text_only() -> None:
    question = AudienceQuestionResponse(
        question_id=UUID(int=98),
        prompt="[DEMO] 問題",
        options=[],
    )

    assert set(question.model_dump()) == {"question_id", "prompt", "options"}


@pytest.mark.parametrize("image_field", ["image_url", "image_alt"])
@pytest.mark.parametrize("value", [None, "retired-image-value"])
def test_audience_question_rejects_retired_image_fields(
    image_field: str,
    value: str | None,
) -> None:
    with pytest.raises(ValidationError) as error_info:
        AudienceQuestionResponse.model_validate(
            {
                "question_id": UUID(int=98),
                "prompt": "[DEMO] 問題",
                "options": [],
                image_field: value,
            }
        )

    assert error_info.value.errors()[0]["type"] == "extra_forbidden"
    assert error_info.value.errors()[0]["loc"] == (image_field,)


def test_answer_status_contract_has_strict_discriminated_variants() -> None:
    unsaved = UnsavedAnswerStatusResponse(
        status="NotStored",
        client_answer_id=CLIENT_ANSWER_ID,
    )

    assert unsaved.model_dump(mode="json") == {
        "status": "NotStored",
        "client_answer_id": str(CLIENT_ANSWER_ID),
    }
    with pytest.raises(ValidationError):
        AcceptedAnswerStatusResponse.model_validate({"status": "AcceptedLocked", "answer": None})


def test_submit_answer_contract_never_accepts_participant_identity() -> None:
    with pytest.raises(ValidationError):
        SubmitAnswerRequest.model_validate(
            {
                "round_version": 2,
                "option_id": str(OPTION_ID),
                "client_answer_id": str(CLIENT_ANSWER_ID),
                "participant_id": str(UUID(int=99)),
            }
        )


def test_participant_join_requires_256_bit_retry_secret() -> None:
    accepted = ParticipantJoinRequest(
        client_join_token=CLIENT_JOIN_VALUE_FIXTURE,
    )

    assert accepted.client_join_token == CLIENT_JOIN_VALUE_FIXTURE
    assert accepted.model_dump() == {"client_join_token": CLIENT_JOIN_VALUE_FIXTURE}
    with pytest.raises(ValidationError):
        ParticipantJoinRequest.model_validate({"client_join_token": "too-short"})

    with pytest.raises(ValidationError):
        SubmitAnswerRequest.model_validate(
            {
                "round_version": "2",
                "option_id": str(OPTION_ID),
                "client_answer_id": str(CLIENT_ANSWER_ID),
            }
        )


@pytest.mark.parametrize(
    "public_label",
    ["ABC", "abc", "AB", "ABCD", "AB1", "A B", "ABC\n", "ＡＢＣ", "ÄBC", "ßbc", ""],
)
def test_participant_join_rejects_client_specified_public_label(public_label: str) -> None:
    with pytest.raises(ValidationError):
        ParticipantJoinRequest.model_validate(
            {"client_join_token": CLIENT_JOIN_VALUE_FIXTURE, "public_label": public_label}
        )


def test_percentage_serializes_as_number_for_zod_contract() -> None:
    option = PublishedOptionResponse(
        option_id=OPTION_ID,
        label="B",
        content="4",
        is_correct=True,
        valid_response_count=3,
        response_percentage=75,
    )

    assert option.model_dump(mode="json")["response_percentage"] == 75.0


def test_participant_ranking_rejects_unrestricted_rows_and_labels() -> None:
    with pytest.raises(ValidationError):
        ParticipantRankingResponse.model_validate(
            {
                "snapshot_version": 1,
                "overall_rank": 1,
                "total_points": 100,
                "correct_count": 1,
                "top_rows": None,
                "public_label": "OTHER-PARTICIPANT",
                "rows": [],
            }
        )


def test_participant_top_rows_are_required_nullable_and_limited_to_third_place() -> None:
    ranking = {"snapshot_version": 2, "overall_rank": 5, "total_points": 600, "correct_count": 2}
    with pytest.raises(ValidationError):
        ParticipantRankingResponse.model_validate(ranking)
    assert ParticipantRankingResponse.model_validate({**ranking, "top_rows": None}).top_rows is None
    assert ParticipantRankingResponse.model_validate({**ranking, "top_rows": []}).top_rows == []
    top_rows = [
        {"public_label": label, "overall_rank": rank, "total_points": 700, "correct_count": 2}
        for label, rank in (("AAA", 1), ("BBB", 2), ("CCC", 3), ("EEE", 3))
    ]
    accepted = ParticipantRankingResponse.model_validate({**ranking, "top_rows": top_rows})
    assert accepted.top_rows is not None
    assert len(accepted.top_rows) == 4
    top_rows[0]["overall_rank"] = 4
    with pytest.raises(ValidationError):
        ParticipantRankingResponse.model_validate({**ranking, "top_rows": top_rows})


def _scored_outcome_payload() -> dict[str, object]:
    return {
        "timing_basis": "server_received_v1",
        "status": "Scored",
        "is_correct": True,
        "base_points": 400,
        "speed_points": 100,
        "total_points": 500,
        "elapsed_ms": 2450,
        "scoring_policy_version": "fixture",
        "speed_cohort": "Correct",
        "speed_rank": 1,
        "speed_cohort_size": 4,
    }


@pytest.mark.parametrize("field", ["base_points", "speed_points", "elapsed_ms"])
@pytest.mark.parametrize("value", [-1, 1.5, "100", True])
def test_personal_score_details_require_nonnegative_integers(field: str, value: object) -> None:
    payload = _scored_outcome_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        ScoredParticipantOutcomeResponse.model_validate(payload)


@pytest.mark.parametrize("field", ["base_points", "speed_points", "elapsed_ms"])
def test_personal_score_details_are_required_even_when_time_is_null(field: str) -> None:
    payload = _scored_outcome_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        ScoredParticipantOutcomeResponse.model_validate(payload)


@pytest.mark.parametrize("elapsed_ms", [None, 0, 2450])
def test_personal_elapsed_time_preserves_zero_and_missing_record(elapsed_ms: int | None) -> None:
    payload = _scored_outcome_payload()
    payload["elapsed_ms"] = elapsed_ms
    outcome = ScoredParticipantOutcomeResponse.model_validate(payload)
    assert outcome.elapsed_ms == elapsed_ms
    assert outcome.model_dump(mode="json")["elapsed_ms"] == elapsed_ms


def test_personal_score_total_must_equal_stored_breakdown() -> None:
    payload = _scored_outcome_payload()
    payload["total_points"] = 501
    with pytest.raises(ValidationError, match="participant_score_breakdown_mismatch"):
        ScoredParticipantOutcomeResponse.model_validate(payload)


@pytest.mark.parametrize("field", ["base_points", "speed_points", "elapsed_ms"])
def test_unanswered_and_common_result_reject_private_score_details(field: str) -> None:
    with pytest.raises(ValidationError):
        UnansweredParticipantOutcomeResponse.model_validate({"status": "Unanswered", field: 0})
    with pytest.raises(ValidationError):
        PublishedResultResponse.model_validate(
            {
                "publication": "Published",
                "correct_option_id": OPTION_ID,
                "distribution": None,
                field: 0,
            }
        )


@pytest.mark.parametrize("field", ["base_points", "speed_points"])
def test_personal_score_components_cannot_be_null(field: str) -> None:
    payload = _scored_outcome_payload()
    payload[field] = None
    with pytest.raises(ValidationError):
        ScoredParticipantOutcomeResponse.model_validate(payload)


@pytest.mark.parametrize("elapsed_ms", [None, -1, 10_000, 14_999, 15_000, 1.5, "1000", True])
def test_client_elapsed_requires_bounded_integer(elapsed_ms: object) -> None:
    with pytest.raises(ValidationError):
        SubmitAnswerRequest.model_validate(
            {
                "round_version": 1,
                "option_id": OPTION_ID,
                "client_answer_id": CLIENT_ANSWER_ID,
                "client_elapsed_ms": elapsed_ms,
            }
        )


@pytest.mark.parametrize("elapsed_ms", [0, 9_999])
def test_client_elapsed_preserves_integer_without_rounding(elapsed_ms: int) -> None:
    request = SubmitAnswerRequest(
        round_version=1,
        option_id=OPTION_ID,
        client_answer_id=CLIENT_ANSWER_ID,
        client_elapsed_ms=elapsed_ms,
    )
    assert request.client_elapsed_ms == elapsed_ms


def test_new_timing_never_falls_back_for_missing_official_time() -> None:
    with pytest.raises(ValidationError, match="client_elapsed_ms_missing"):
        ParticipantAnswerResponse(
            answer_id=UUID(int=30),
            round_id=ROUND_ID,
            option_id=OPTION_ID,
            client_answer_id=CLIENT_ANSWER_ID,
            received_at=PROJECTED_AT,
            timing_basis="client_monotonic_v1",
            client_elapsed_ms=None,
        )
    payload = _scored_outcome_payload()
    payload.update(timing_basis="client_monotonic_v1", elapsed_ms=None)
    with pytest.raises(ValidationError, match="client_elapsed_ms_missing"):
        ScoredParticipantOutcomeResponse.model_validate(payload)
