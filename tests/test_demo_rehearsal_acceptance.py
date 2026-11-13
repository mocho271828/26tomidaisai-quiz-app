from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from queue import SimpleQueue
from urllib.error import URLError
from uuid import UUID

import pytest
from pydantic import BaseModel

from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AnswerReceiptResponse,
    ParticipantJoinRequest,
    ParticipantStateResponse,
)
from quiz_app.operator.control_contracts import (
    OperatorRoundStatisticsResponse,
    OperatorStateResponse,
)
from validation.demo_rehearsal_acceptance import DemoRehearsal
from validation.demo_rehearsal_support import (
    ServerTimeAnchor,
    answer_option_plan,
    assert_preview_is_hidden,
    choose_next_preview,
    choose_switch_question,
    expected_option_counts,
    participant_count,
    validate_participant_questions,
    validate_saved_answer,
    validate_statistics,
)
from validation.local_demo_acceptance import (
    JsonClient,
    ValidationFailure,
    parse_local_base_url,
    request_participant_join,
)

EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("60000000-0000-4000-8000-000000000007")
QUESTION_IDS = [UUID(int=index) for index in range(101, 104)]
OPTION_IDS = [UUID(int=index) for index in range(201, 209)]
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _question_payload(index: int) -> dict[str, object]:
    return {
        "question_id": str(QUESTION_IDS[index]),
        "version": index + 1,
        "display_order": index + 1,
        "prompt": f"[DEMO] question {index + 1}",
        "options": [
            {
                "option_id": str(OPTION_IDS[index * 2]),
                "label": "A",
                "content": "alpha",
                "is_correct": True,
            },
            {
                "option_id": str(OPTION_IDS[index * 2 + 1]),
                "label": "B",
                "content": "beta",
                "is_correct": False,
            },
        ],
    }


def _operator_state(current_question_index: int) -> OperatorStateResponse:
    question = _question_payload(current_question_index)
    question_without_display_order = {
        key: value for key, value in question.items() if key != "display_order"
    }
    payload: dict[str, object] = {
        "award_ceremony_phase": None,
        "event_id": str(EVENT_ID),
        "projected_at": NOW.isoformat(),
        "event_title": "[DEMO] acceptance",
        "event_state": "LobbyOpen",
        "event_version": 1,
        "result_visibility": {
            "distribution": True,
            "personal_ranking": False,
            "overall_ranking": False,
        },
        "start_delay_seconds": 5,
        "visibility_controls": {
            target: {"mode": "manual", "initially_visible": visible, "changes": ()}
            for target, visible in (
                ("distribution", True),
                ("personal_ranking", False),
                ("overall_ranking", False),
            )
        },
        "participant_count": 0,
        "available_questions": [_question_payload(index) for index in range(3)],
        "current_round": {
            "round_id": str(ROUND_ID),
            "version": 1,
            "state_version": 4,
            "state": "PreparingAssets",
            "open_at": None,
            "standard_close_at": None,
            "result_publication": "Withheld",
            "result_failure_code": None,
            "can_publish_results": False,
            "statistics": {
                "answer_count": 0,
                "average_response_ms": None,
                "options": [
                    {
                        "option_id": option["option_id"],
                        "answer_count": 0,
                        "average_response_ms": None,
                    }
                    for option in question["options"]
                ],
            },
            "question": question_without_display_order,
        },
        "latest_ranking_snapshot_version": None,
    }
    # mainには存在しない先行branch fieldをtest fixtureの契約判定だけに吸収する。
    if "used_question_ids" in OperatorStateResponse.model_fields:
        payload["used_question_ids"] = [str(QUESTION_IDS[current_question_index])]
    return OperatorStateResponse.model_validate(payload)


def _participant_state(current_question_index: int) -> ParticipantStateResponse:
    question = _question_payload(current_question_index)
    return ParticipantStateResponse.model_validate(
        {
            "award_ceremony": None,
            "projected_at": NOW.isoformat(),
            "event": {
                "timing_basis": "server_received_v1",
                "arrival_grace_ms": 0,
                "clock_tolerance_ms": 0,
                "client_ahead_tolerance_ms": 0,
                "event_id": str(EVENT_ID),
                "event_title": "[DEMO] acceptance",
                "event_state": "LobbyOpen",
                "event_version": 1,
                "start_delay_seconds": 3,
            },
            "participant": {
                "public_label": "AAA",
                "is_ranking_eligible": True,
            },
            "current_round": {
                "round_id": str(ROUND_ID),
                "round_number": 1,
                "version": 1,
                "state_version": 4,
                "state": "PreparingAssets",
                "open_at": None,
                "standard_close_at": None,
                "question": {
                    "question_id": question["question_id"],
                    "prompt": question["prompt"],
                    "options": [
                        {
                            "option_id": option["option_id"],
                            "label": option["label"],
                            "content": option["content"],
                        }
                        for option in question["options"]
                    ],
                },
            },
            "latest_result": None,
            "result_visibility": {
                "distribution": False,
                "personal_ranking": False,
                "overall_ranking": False,
            },
            "latest_ranking_snapshot_version": None,
            "personal_result_delivery": {"status": "none", "revision": None},
        }
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.20:8080",
        "http://10.20.30.40:8080",
        "http://172.16.2.3:8080",
        "http://[fd00::20]:8080",
    ],
)
def test_private_lan_url_is_only_allowed_with_explicit_opt_in(base_url: str) -> None:
    with pytest.raises(ValidationFailure, match="approved local gateway"):
        parse_local_base_url(base_url)

    parsed = parse_local_base_url(base_url, allow_private_lan=True)

    assert parsed.port == 8080
    JsonClient(base_url, allow_private_lan=True)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://8.8.8.8:8080",
        "http://example.test:8080",
        "http://192.168.1.20:8080/path",
        "http://user@192.168.1.20:8080",
        "http://0.0.0.0:8080",
    ],
)
def test_lan_url_rejects_public_ambiguous_or_non_root_targets(base_url: str) -> None:
    with pytest.raises(ValidationFailure, match="approved local gateway"):
        parse_local_base_url(base_url, allow_private_lan=True)


def test_response_validation_error_does_not_expose_external_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpectedResponse(BaseModel):
        count: int

    client = JsonClient("http://gateway:8080")

    def invalid_response(
        _path: str,
        *,
        method: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        del method, body, headers
        return b'{"count":"participant-secret-value"}'

    monkeypatch.setattr(client, "_request", invalid_response)

    with pytest.raises(ValidationFailure, match="ExpectedResponse response schema") as error:
        client.request_model("/api/test", ExpectedResponse)

    assert "participant-secret-value" not in str(error.value)


@pytest.mark.parametrize("value", ["1", "33", "eight"])
def test_participant_count_rejects_values_outside_rehearsal_scope(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        participant_count(value)


def test_acceptance_refuses_a_round_that_has_already_started_preparation() -> None:
    state = _operator_state(current_question_index=0)
    current_round = state.current_round
    assert current_round is not None
    state.current_round = current_round.model_copy(update={"state": "ReadyToSchedule"})

    with pytest.raises(ValidationFailure, match="will not reset, seed, or migrate"):
        DemoRehearsal._validate_initial_state(state)


def test_acceptance_refuses_an_event_with_existing_participants() -> None:
    state = _operator_state(current_question_index=0).model_copy(update={"participant_count": 1})

    with pytest.raises(ValidationFailure, match="will not reset, seed, or migrate"):
        DemoRehearsal._validate_initial_state(state)


def test_participant_join_retries_once_with_the_same_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = JsonClient("http://localhost")
    request = ParticipantJoinRequest(
        client_join_token="A" * 43,
    )
    seen_requests: list[ParticipantJoinRequest] = []

    def scripted_request(*_: object, **kwargs: object) -> ParticipantStateResponse:
        candidate = kwargs.get("request_model")
        assert isinstance(candidate, ParticipantJoinRequest)
        seen_requests.append(candidate)
        if len(seen_requests) == 1:
            raise URLError("response lost")
        return _participant_state(0)

    monkeypatch.setattr(client, "request_model", scripted_request)

    state = request_participant_join(client, request, retry_delay_seconds=0)

    assert state.participant.public_label == "AAA"
    assert seen_requests == [request, request]
    assert all(set(item.model_dump()) == {"client_join_token"} for item in seen_requests)


@pytest.mark.parametrize("labels", [("XYZ", "ABC"), ("XYZ", "XYZ")])
def test_rehearsal_checks_server_assigned_labels_are_unique(
    monkeypatch: pytest.MonkeyPatch,
    labels: tuple[str, str],
) -> None:
    responses: SimpleQueue[ParticipantStateResponse] = SimpleQueue()
    for label in labels:
        state = _participant_state(0)
        state.participant.public_label = label
        responses.put(state)

    def scripted_join(
        _client: JsonClient,
        request: ParticipantJoinRequest,
    ) -> ParticipantStateResponse:
        assert set(request.model_dump()) == {"client_join_token"}
        return responses.get_nowait()

    monkeypatch.setattr(
        "validation.demo_rehearsal_acceptance.request_participant_join", scripted_join
    )
    rehearsal = DemoRehearsal("http://localhost", "http://localhost", len(labels))

    if len(set(labels)) != len(labels):
        with pytest.raises(ValidationFailure, match="duplicate public labels"):
            rehearsal._join_participants()
    else:
        participants = rehearsal._join_participants()
        assert {actor.state.participant.public_label for actor in participants} == set(labels)


def test_question_selection_uses_only_an_unused_non_current_candidate() -> None:
    initial = _operator_state(current_question_index=0)
    switched = _operator_state(current_question_index=1)

    switch_target = choose_switch_question(initial)
    next_preview = choose_next_preview(
        switched,
        excluded_question_ids={QUESTION_IDS[0]},
    )

    assert switch_target.question_id == QUESTION_IDS[1]
    assert next_preview.question_id == QUESTION_IDS[2]
    assert sum(option.is_correct for option in next_preview.options) == 1


def test_participant_projection_must_match_switched_question() -> None:
    operator_state = _operator_state(current_question_index=1)
    operator_round = operator_state.current_round
    assert operator_round is not None

    validate_participant_questions([_participant_state(1)], operator_round)

    with pytest.raises(ValidationFailure, match="switched current question"):
        validate_participant_questions([_participant_state(0)], operator_round)


def test_answer_receipt_and_status_must_match_the_submitted_identity() -> None:
    answer_id = UUID(int=301)
    client_answer_id = UUID(int=302)
    receipt = AnswerReceiptResponse(
        answer_id=answer_id,
        round_id=ROUND_ID,
        option_id=OPTION_IDS[0],
        client_answer_id=client_answer_id,
        received_at=NOW,
        is_replay=False,
        client_elapsed_ms=2000,
        timing_basis="client_monotonic_v1",
    )
    status = AcceptedAnswerStatusResponse(
        status="AcceptedLocked",
        answer=receipt.model_dump(exclude={"is_replay"}),
    )

    validate_saved_answer(
        receipt,
        status,
        round_id=ROUND_ID,
        option_id=OPTION_IDS[0],
        client_answer_id=client_answer_id,
        client_elapsed_ms=2000,
    )

    with pytest.raises(ValidationFailure, match="did not match its plan"):
        validate_saved_answer(
            receipt,
            status,
            round_id=ROUND_ID,
            option_id=OPTION_IDS[1],
            client_answer_id=client_answer_id,
            client_elapsed_ms=2000,
        )

    with pytest.raises(ValidationFailure, match="did not match its plan"):
        validate_saved_answer(
            receipt,
            status,
            round_id=ROUND_ID,
            option_id=OPTION_IDS[0],
            client_answer_id=client_answer_id,
            client_elapsed_ms=3500,
        )


def test_local_rehearsal_clock_advances_monotonically_from_server_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = ServerTimeAnchor(NOW, 10)
    monkeypatch.setattr("validation.demo_rehearsal_support.time.monotonic", lambda: 10.375)
    assert anchor.elapsed_ms(NOW - timedelta(seconds=2)) == 2375
    with pytest.raises(ValidationFailure, match="before round opening"):
        anchor.elapsed_ms(NOW + timedelta(seconds=1))


def test_expected_counts_and_operator_statistics_match_each_option() -> None:
    option_ids = OPTION_IDS[:4]
    selected = answer_option_plan(option_ids, 8)
    expected = expected_option_counts(option_ids, selected)
    statistics = OperatorRoundStatisticsResponse.model_validate(
        {
            "answer_count": 8,
            "average_response_ms": 600,
            "options": [
                {
                    "option_id": str(option_id),
                    "answer_count": answer_count,
                    "average_response_ms": 600 + index,
                }
                for index, (option_id, answer_count) in enumerate(
                    zip(option_ids, expected, strict=True)
                )
            ],
        }
    )

    assert expected == (3, 2, 2, 1)

    validate_statistics(statistics, option_ids, expected)

    with pytest.raises(ValidationFailure, match="per-option"):
        validate_statistics(
            statistics.model_copy(
                update={
                    "options": [
                        statistics.options[0].model_copy(update={"answer_count": 2}),
                        statistics.options[1].model_copy(update={"answer_count": 3}),
                        *statistics.options[2:],
                    ]
                }
            ),
            option_ids,
            expected,
        )


def test_preview_confidentiality_detects_candidate_identity_in_participant_payload() -> None:
    state = _operator_state(current_question_index=0)
    preview = state.available_questions[1]
    participant_state = ParticipantStateResponse.model_validate(
        {
            "award_ceremony": None,
            "projected_at": NOW.isoformat(),
            "event": {
                "timing_basis": "server_received_v1",
                "arrival_grace_ms": 0,
                "clock_tolerance_ms": 0,
                "client_ahead_tolerance_ms": 0,
                "event_id": str(EVENT_ID),
                "event_title": "[DEMO] acceptance",
                "event_state": "LobbyOpen",
                "event_version": 1,
                "start_delay_seconds": 3,
            },
            "participant": {
                "public_label": "AAA",
                "is_ranking_eligible": True,
            },
            "current_round": {
                "round_id": str(ROUND_ID),
                "round_number": 1,
                "version": 1,
                "state_version": 4,
                "state": "PreparingAssets",
                "open_at": None,
                "standard_close_at": None,
                "question": {
                    "question_id": str(preview.question_id),
                    "prompt": preview.prompt,
                    "options": [
                        {
                            "option_id": str(option.option_id),
                            "label": option.label,
                            "content": option.content,
                        }
                        for option in preview.options
                    ],
                },
            },
            "latest_result": None,
            "result_visibility": {
                "distribution": False,
                "personal_ranking": False,
                "overall_ranking": False,
            },
            "latest_ranking_snapshot_version": None,
            "personal_result_delivery": {"status": "none", "revision": None},
        }
    )
    with pytest.raises(ValidationFailure, match="leaked"):
        assert_preview_is_hidden(preview, [participant_state])


@pytest.mark.parametrize("unready_state", ["AwaitingValidation", "ReadyToReveal"])
def test_rehearsal_publishes_only_after_explicit_operator_capability(
    monkeypatch: pytest.MonkeyPatch, unready_state: str
) -> None:
    from quiz_app.models.enums import RoundState
    from quiz_app.operator.control_contracts import (
        OperatorMutationRequest,
        OperatorMutationResponse,
    )
    from validation import demo_rehearsal_acceptance as acceptance

    state = _operator_state(0)
    round_ = state.current_round
    assert round_ is not None
    round_.state = RoundState.READY_TO_REVEAL
    round_.can_publish_results = True
    round_.statistics.answer_count = 2
    for option in round_.statistics.options:
        option.answer_count = 1
    pending = state.model_copy(
        update={
            "current_round": round_.model_copy(
                update={"state": RoundState(unready_state), "can_publish_results": False}
            )
        }
    )
    responses = iter((pending, state))
    mutations: list[tuple[str, OperatorMutationRequest]] = []
    monkeypatch.setattr(acceptance, "operator_state", lambda _client: next(responses))
    monkeypatch.setattr(acceptance, "wait_for_server_time", lambda *_args: None)
    monkeypatch.setattr(acceptance.time, "sleep", lambda _seconds: None)

    def mutate(_client: JsonClient, path: str, request: OperatorMutationRequest) -> None:
        mutations.append((path, request))

    monkeypatch.setattr(acceptance, "mutate_round", mutate)
    scheduled = OperatorMutationResponse(
        operation="schedule_round",
        event_id=EVENT_ID,
        event_version=1,
        round_id=ROUND_ID,
        round_version=1,
        round_state=RoundState.SCHEDULED,
        result_visibility=state.result_visibility,
        open_at=NOW,
        standard_close_at=NOW + timedelta(seconds=10),
    )
    DemoRehearsal("http://localhost:8080", "http://localhost:8080", 2)._publish_aggregated_results(
        scheduled, state
    )
    assert len(mutations) == 1
    path, request = mutations[0]
    assert path == f"/api/v1/operator/rounds/{ROUND_ID}/publish-results"
    assert request.expected_version == round_.version
    assert "reason" not in request.model_dump()
