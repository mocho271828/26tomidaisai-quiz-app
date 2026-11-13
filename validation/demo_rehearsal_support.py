from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AnswerReceiptResponse,
    ParticipantStateResponse,
    TimeResponse,
)
from quiz_app.operator.contracts import OperatorLoginRequest, OperatorSessionResponse
from quiz_app.operator.control_contracts import (
    OperatorMutationRequest,
    OperatorMutationResponse,
    OperatorQuestionPreviewResponse,
    OperatorRoundResponse,
    OperatorRoundStatisticsResponse,
    OperatorStateResponse,
)
from validation.local_demo_acceptance import JsonClient, ValidationFailure

EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
MIN_PARTICIPANT_COUNT = 2
MAX_PARTICIPANT_COUNT = 32
SERVER_TIME_POLL_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class ParticipantActor:
    client: JsonClient
    state: ParticipantStateResponse


@dataclass(frozen=True, slots=True)
class SavedAnswer:
    actor: ParticipantActor
    client_answer_id: UUID
    receipt: AnswerReceiptResponse
    client_elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ServerTimeAnchor:
    server_time: datetime
    monotonic_time: float

    def elapsed_ms(self, open_at: datetime) -> int:
        """server時刻へ対応付けたローカル単調時計で、選択時の整数msを確定する。"""

        elapsed_seconds = (
            (self.server_time - open_at).total_seconds() + time.monotonic() - self.monotonic_time
        )
        if elapsed_seconds < 0:
            raise ValidationFailure("Local answer clock is before round opening")
        return int(elapsed_seconds * 1000)


@dataclass(frozen=True, slots=True)
class RehearsalResult:
    participant_count: int
    option_counts: tuple[int, ...]


def participant_count(value: str) -> int:
    """当日確認の範囲を越える負荷値をrunnerへ渡さない。"""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("participants must be an integer") from error
    if not MIN_PARTICIPANT_COUNT <= parsed <= MAX_PARTICIPANT_COUNT:
        raise argparse.ArgumentTypeError(
            f"participants must be between {MIN_PARTICIPANT_COUNT} and {MAX_PARTICIPANT_COUNT}"
        )
    return parsed


def require_current_round(state: OperatorStateResponse) -> OperatorRoundResponse:
    if state.current_round is None:
        raise ValidationFailure("The demo event has no current round")
    return state.current_round


def _unused_questions(
    state: OperatorStateResponse,
) -> list[OperatorQuestionPreviewResponse]:
    current_round = require_current_round(state)
    return [
        question
        for question in state.available_questions
        if question.question_id != current_round.question.question_id
    ]


def choose_switch_question(
    state: OperatorStateResponse,
) -> OperatorQuestionPreviewResponse:
    """未使用かつ現在問以外の先頭候補を、切替確認対象にする。"""

    candidates = _unused_questions(state)
    if not candidates:
        raise ValidationFailure("No unused question is available for the switch acceptance")
    return candidates[0]


def choose_next_preview(
    state: OperatorStateResponse,
    excluded_question_ids: set[UUID] | None = None,
) -> OperatorQuestionPreviewResponse:
    """切替後も運営だけが確認できる未使用候補を選ぶ。"""

    excluded = excluded_question_ids or set()
    candidates = [
        question for question in _unused_questions(state) if question.question_id not in excluded
    ]
    if not candidates:
        raise ValidationFailure("No unused question is available for the next-question preview")
    preview = candidates[0]
    if len(preview.options) < 2 or sum(option.is_correct for option in preview.options) != 1:
        raise ValidationFailure("Operator next-question preview is incomplete")
    return preview


def expected_option_counts(
    option_ids: list[UUID],
    selected_option_ids: list[UUID],
) -> tuple[int, ...]:
    counts = Counter(selected_option_ids)
    if set(counts) - set(option_ids):
        raise ValidationFailure("Answer plan contains an option outside the current question")
    return tuple(counts[option_id] for option_id in option_ids)


def answer_option_plan(option_ids: list[UUID], answer_count: int) -> list[UUID]:
    """選択肢の取違えを統計で検出できる非対称な回答分布を作る。"""

    if not option_ids or answer_count < 1:
        raise ValidationFailure("Answer plan requires options and participants")
    return [
        option_ids[0],
        *(option_ids[index % len(option_ids)] for index in range(answer_count - 1)),
    ]


def validate_participant_questions(
    states: list[ParticipantStateResponse],
    operator_round: OperatorRoundResponse,
) -> None:
    expected_option_ids = {option.option_id for option in operator_round.question.options}
    for state in states:
        participant_round = state.current_round
        if (
            participant_round is None
            or participant_round.question is None
            or participant_round.round_id != operator_round.round_id
            or participant_round.version != operator_round.version
            or participant_round.question.question_id != operator_round.question.question_id
            or {option.option_id for option in participant_round.question.options}
            != expected_option_ids
            or state.answer is not None
        ):
            raise ValidationFailure(
                "A participant projection did not match the switched current question"
            )


def validate_saved_answer(
    receipt: AnswerReceiptResponse,
    status: AcceptedAnswerStatusResponse,
    *,
    round_id: UUID,
    option_id: UUID,
    client_answer_id: UUID,
    client_elapsed_ms: int,
) -> None:
    expected_identity = (round_id, option_id, client_answer_id)
    if (
        receipt.is_replay
        or (receipt.round_id, receipt.option_id, receipt.client_answer_id) != expected_identity
        or status.status != "AcceptedLocked"
        or status.answer.answer_id != receipt.answer_id
        or (
            status.answer.round_id,
            status.answer.option_id,
            status.answer.client_answer_id,
        )
        != expected_identity
        or receipt.timing_basis != "client_monotonic_v1"
        or status.answer.timing_basis != "client_monotonic_v1"
        or receipt.client_elapsed_ms != client_elapsed_ms
        or status.answer.client_elapsed_ms != client_elapsed_ms
    ):
        raise ValidationFailure("A participant answer receipt or status did not match its plan")


def validate_statistics(
    statistics: OperatorRoundStatisticsResponse,
    option_ids: list[UUID],
    expected_counts: tuple[int, ...],
) -> None:
    if statistics.answer_count != sum(expected_counts):
        raise ValidationFailure("Operator answer total does not match saved answers")
    actual_by_option = {option.option_id: option for option in statistics.options}
    if set(actual_by_option) != set(option_ids):
        raise ValidationFailure("Operator statistics option identities are incomplete")
    for option_id, expected_count in zip(option_ids, expected_counts, strict=True):
        option_statistics = actual_by_option[option_id]
        if option_statistics.answer_count != expected_count:
            raise ValidationFailure("Operator per-option answer counts do not match")
        if expected_count > 0 and option_statistics.average_response_ms is None:
            raise ValidationFailure("Operator answer timing average is missing")


def assert_preview_is_hidden(
    preview: OperatorQuestionPreviewResponse,
    states: list[ParticipantStateResponse],
) -> None:
    forbidden_ids = {
        str(preview.question_id),
        *(str(option.option_id) for option in preview.options),
    }
    for state in states:
        payload = state.model_dump_json()
        if any(identifier in payload for identifier in forbidden_ids):
            raise ValidationFailure("Next-question preview leaked outside the operator projection")


def validate_published_projections(
    published_state: OperatorStateResponse,
    participant_states: list[ParticipantStateResponse],
    saved_answers: list[SavedAnswer],
    expected_counts: tuple[int, ...],
    preview: OperatorQuestionPreviewResponse,
) -> None:
    round_ = require_current_round(published_state)
    option_ids = [option.option_id for option in round_.question.options]
    if round_.state != "Reveal" or round_.result_publication != "Published":
        raise ValidationFailure("Operator projection did not reach published Reveal")
    validate_statistics(round_.statistics, option_ids, expected_counts)

    for saved, participant_state in zip(saved_answers, participant_states, strict=True):
        participant_result = participant_state.latest_result
        if (
            participant_state.current_round is not None
            or participant_state.answer is not None
            or participant_result is None
            or participant_result.round_id != round_.round_id
            or participant_result.question.question_id != round_.question.question_id
            or participant_result.selected_option_id != saved.receipt.option_id
            or participant_result.result.outcome.status != "Scored"
        ):
            raise ValidationFailure("A participant projection did not confirm its result")
        distribution = participant_result.result.distribution
        if (distribution is not None) != published_state.result_visibility.distribution:
            raise ValidationFailure("Participant distribution visibility does not match policy")
        if distribution is not None:
            counts_by_option = {
                option.option_id: option.valid_response_count for option in distribution.options
            }
            if (
                distribution.valid_answer_count != len(saved_answers)
                or set(counts_by_option) != set(option_ids)
                or tuple(counts_by_option[option_id] for option_id in option_ids) != expected_counts
            ):
                raise ValidationFailure(
                    "Participant option distribution does not match operator statistics"
                )
        outcome = participant_result.result.outcome
        if not published_state.result_visibility.personal_ranking and (
            participant_state.ranking is not None
            or outcome.speed_rank is not None
            or outcome.speed_cohort_size is not None
        ):
            raise ValidationFailure("Participant ranking was exposed while hidden")

    published_preview = next(
        (
            candidate
            for candidate in published_state.available_questions
            if candidate.question_id == preview.question_id
            and candidate.question_id != round_.question.question_id
        ),
        None,
    )
    if published_preview != preview:
        raise ValidationFailure("The next-question preview changed after result publication")
    assert_preview_is_hidden(preview, participant_states)


def authenticate_operator(base_url: str) -> JsonClient:
    username = os.environ.get("DEMO_OPERATOR_USERNAME")
    password = os.environ.get("DEMO_OPERATOR_PASSWORD")
    if not username or not password:
        raise ValidationFailure("DEMO_OPERATOR_USERNAME and DEMO_OPERATOR_PASSWORD are required")

    try:
        request = OperatorLoginRequest(username=username, password=password)
    except ValidationError as error:
        raise ValidationFailure(
            f"Operator credentials failed schema validation ({error.error_count()} errors)"
        ) from None
    body = json.dumps(
        {
            "username": request.username,
            "password": request.password.get_secret_value(),
        }
    ).encode()
    client = JsonClient(base_url)
    client.request_model(
        "/api/v1/operator/auth/login",
        OperatorSessionResponse,
        method="POST",
        request_body=body,
    )
    client.request_model("/api/v1/operator/auth/session", OperatorSessionResponse)
    return client


def operator_state(operator: JsonClient) -> OperatorStateResponse:
    return operator.request_model(
        "/api/v1/operator/state",
        OperatorStateResponse,
    )


def mutate_round(
    operator: JsonClient,
    path: str,
    request: OperatorMutationRequest,
) -> OperatorMutationResponse:
    return operator.request_model(
        path,
        OperatorMutationResponse,
        method="POST",
        request_model=request,
        headers={"X-CSRF-Token": operator.cookie_value("quiz_operator_csrf")},
    )


def wait_for_server_time(client: JsonClient, target: datetime) -> ServerTimeAnchor:
    """端末wall clockを使わず、server時刻が目標へ達するまで待つ。"""

    initial_time = client.request_model("/api/v1/time", TimeResponse).server_time
    deadline = time.monotonic() + max(1.0, (target - initial_time).total_seconds() + 3)
    while time.monotonic() < deadline:
        requested_at = time.monotonic()
        server_time = client.request_model("/api/v1/time", TimeResponse).server_time
        received_at = time.monotonic()
        if server_time >= target:
            return ServerTimeAnchor(server_time, (requested_at + received_at) / 2)
        remaining = max((target - server_time).total_seconds(), 0.01)
        time.sleep(min(SERVER_TIME_POLL_SECONDS, remaining))
    raise ValidationFailure("Server time did not reach the expected round boundary")
