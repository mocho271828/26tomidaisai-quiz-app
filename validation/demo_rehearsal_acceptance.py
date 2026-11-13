from __future__ import annotations

import argparse
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from uuid import UUID, uuid4

import logfire
from pydantic import ValidationError

from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AnswerReceiptResponse,
    ParticipantJoinRequest,
    ParticipantStateResponse,
    SubmitAnswerRequest,
)
from quiz_app.contracts import ReadinessHealthResponse
from quiz_app.operator.control_contracts import (
    OperatorMutationRequest,
    OperatorMutationResponse,
    OperatorQuestionPreviewResponse,
    OperatorRoundResponse,
    OperatorStateResponse,
    QuestionSelectionMutationRequest,
)
from validation.demo_rehearsal_support import (
    ParticipantActor,
    RehearsalResult,
    SavedAnswer,
    answer_option_plan,
    assert_preview_is_hidden,
    authenticate_operator,
    choose_next_preview,
    choose_switch_question,
    expected_option_counts,
    mutate_round,
    operator_state,
    participant_count,
    require_current_round,
    validate_participant_questions,
    validate_published_projections,
    validate_saved_answer,
    validate_statistics,
    wait_for_server_time,
)
from validation.local_demo_acceptance import (
    JsonClient,
    ValidationFailure,
    request_participant_join,
)

EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
DEFAULT_PARTICIPANT_COUNT = 8
STATE_POLL_SECONDS = 0.1
STATE_POLL_TIMEOUT_SECONDS = 5
OPEN_SAFETY_MARGIN_MS = 100
CLOSE_SAFETY_MARGIN_MS = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="準備済みlocal demoを複数参加者で一回だけ進行して受入確認する"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LOCAL_DEMO_BASE_URL", "http://gateway:8080"),
        help="運営資格情報を送るCompose内またはloopbackのCaddy URL",
    )
    parser.add_argument(
        "--participant-base-url",
        default=os.environ.get("LOCAL_DEMO_PARTICIPANT_BASE_URL"),
        help="参加者が通るprivate LAN URL。省略時は--base-urlと同じ",
    )
    parser.add_argument(
        "--participants",
        default=os.environ.get("DEMO_PARTICIPANT_COUNT", str(DEFAULT_PARTICIPANT_COUNT)),
        type=participant_count,
    )
    return parser.parse_args()


class DemoRehearsal:
    """既存APIだけを使い、複数参加者の一問分を通しで検証する。"""

    def __init__(self, base_url: str, participant_base_url: str, count: int) -> None:
        self._base_url = base_url
        self._participant_base_url = participant_base_url
        self._count = count
        self._operator = JsonClient(base_url)

    def run(self) -> RehearsalResult:
        self._validate_entrypoints()
        self._operator = authenticate_operator(self._base_url)
        initial_state = operator_state(self._operator)
        initial_round = self._validate_initial_state(initial_state)
        participants = self._join_participants()
        joined_state = operator_state(self._operator)
        if joined_state.participant_count != initial_state.participant_count + self._count:
            raise ValidationFailure("Participant count did not increase by the requested amount")
        logfire.info("demo acceptance participants joined", participant_count=self._count)

        switched_state, preview = self._switch_question(initial_state, initial_round)
        participant_states = [self._session(actor) for actor in participants]
        validate_participant_questions(
            participant_states,
            require_current_round(switched_state),
        )
        assert_preview_is_hidden(
            preview,
            [
                *(actor.state for actor in participants),
                *participant_states,
            ],
        )

        ready_state = self._prepare(participants, switched_state)
        logfire.info("demo acceptance question delivery verified", participant_count=self._count)
        scheduled = self._schedule(ready_state)
        saved_answers, expected_counts = self._answer(participants, scheduled)
        logfire.info(
            "demo acceptance answers saved",
            answer_count=len(saved_answers),
            option_counts=expected_counts,
        )
        statistics_state = self._wait_for_statistics(expected_counts)
        self._publish_aggregated_results(scheduled, statistics_state)
        self._validate_published(saved_answers, expected_counts, preview)
        logfire.info("demo acceptance results verified", answer_count=self._count)
        return RehearsalResult(self._count, expected_counts)

    def _validate_entrypoints(self) -> None:
        control = JsonClient(self._base_url)
        participant = JsonClient(self._participant_base_url, allow_private_lan=True)
        control.get_html("/operator")
        participant.get_html("/")
        for client in (control, participant):
            readiness = client.request_model("/api/health/ready", ReadinessHealthResponse)
            if (
                readiness.status,
                readiness.database,
                readiness.migration,
                readiness.redis,
            ) != ("ready", "ok", "current", "ok"):
                raise ValidationFailure("Local demo readiness is not fully ready")

    @staticmethod
    def _validate_initial_state(state: OperatorStateResponse) -> OperatorRoundResponse:
        round_ = require_current_round(state)
        if (
            state.event_state != "LobbyOpen"
            or round_.state != "PreparingAssets"
            or round_.result_publication != "Withheld"
            or round_.statistics.answer_count != 0
            or state.participant_count != 0
        ):
            raise ValidationFailure(
                "Acceptance requires an unused PreparingAssets demo round; "
                "the runner will not reset, seed, or migrate the database"
            )
        if len(state.available_questions) < 3:
            raise ValidationFailure("Acceptance requires at least three demo questions")
        return round_

    def _join_participants(self) -> list[ParticipantActor]:
        def join(_index: int) -> ParticipantActor:
            join_request = ParticipantJoinRequest(
                client_join_token=secrets.token_urlsafe(32),
            )
            client = JsonClient(self._participant_base_url, allow_private_lan=True)
            state = request_participant_join(client, join_request)
            if state.event.event_id != EVENT_ID:
                raise ValidationFailure("A participant joined an unexpected event")
            return ParticipantActor(client, state)

        with ThreadPoolExecutor(max_workers=self._count) as executor:
            participants = list(executor.map(join, range(self._count)))
        if len({actor.state.participant.public_label for actor in participants}) != self._count:
            raise ValidationFailure("Participants received duplicate public labels")
        return participants

    def _switch_question(
        self,
        initial_state: OperatorStateResponse,
        initial_round: OperatorRoundResponse,
    ) -> tuple[OperatorStateResponse, OperatorQuestionPreviewResponse]:
        target = choose_switch_question(initial_state)
        mutate_round(
            self._operator,
            f"/api/v1/operator/rounds/{initial_round.round_id}/question",
            QuestionSelectionMutationRequest(
                expected_version=initial_round.version,
                idempotency_key=uuid4(),
                question_id=target.question_id,
            ),
        )
        state = operator_state(self._operator)
        if require_current_round(state).question.question_id != target.question_id:
            raise ValidationFailure("The operator question switch was not persisted")
        return state, choose_next_preview(
            state,
            excluded_question_ids={initial_round.question.question_id},
        )

    @staticmethod
    def _session(actor: ParticipantActor) -> ParticipantStateResponse:
        return actor.client.request_model(
            "/api/v1/participant/session",
            ParticipantStateResponse,
        )

    def _prepare(
        self,
        participants: list[ParticipantActor],
        switched_state: OperatorStateResponse,
    ) -> OperatorStateResponse:
        round_ = require_current_round(switched_state)
        mutate_round(
            self._operator,
            f"/api/v1/operator/rounds/{round_.round_id}/prepare",
            OperatorMutationRequest(
                expected_version=round_.version,
                idempotency_key=uuid4(),
            ),
        )
        ready_state = operator_state(self._operator)
        if require_current_round(ready_state).state != "ReadyToSchedule":
            raise ValidationFailure("Operator preparation did not allow scheduling")
        validate_participant_questions(
            [self._session(actor) for actor in participants],
            require_current_round(ready_state),
        )
        return ready_state

    def _schedule(self, ready_state: OperatorStateResponse) -> OperatorMutationResponse:
        round_ = require_current_round(ready_state)
        result = mutate_round(
            self._operator,
            f"/api/v1/operator/rounds/{round_.round_id}/schedule",
            OperatorMutationRequest(
                expected_version=round_.version,
                idempotency_key=uuid4(),
            ),
        )
        if (
            result.round_state != "Scheduled"
            or result.round_version is None
            or result.open_at is None
            or result.standard_close_at is None
        ):
            raise ValidationFailure("Round schedule response is incomplete")
        return result

    def _answer(
        self,
        participants: list[ParticipantActor],
        scheduled: OperatorMutationResponse,
    ) -> tuple[list[SavedAnswer], tuple[int, ...]]:
        if (
            scheduled.round_id is None
            or scheduled.round_version is None
            or scheduled.open_at is None
        ):
            raise ValidationFailure("Scheduled round identity is incomplete")
        current_round = require_current_round(operator_state(self._operator))
        option_ids = [option.option_id for option in current_round.question.options]
        if len(option_ids) < 2:
            raise ValidationFailure("Current question does not have enough answer options")
        selected_option_ids = answer_option_plan(option_ids, len(participants))
        planned = list(zip(participants, selected_option_ids, strict=True))
        clock_anchor = wait_for_server_time(
            JsonClient(self._base_url),
            scheduled.open_at + timedelta(milliseconds=OPEN_SAFETY_MARGIN_MS),
        )

        def submit(item: tuple[ParticipantActor, UUID]) -> SavedAnswer:
            actor, option_id = item
            client_answer_id = uuid4()
            client_elapsed_ms = clock_anchor.elapsed_ms(scheduled.open_at)
            receipt = actor.client.request_model(
                f"/api/v1/participant/rounds/{scheduled.round_id}/answers",
                AnswerReceiptResponse,
                method="POST",
                request_model=SubmitAnswerRequest(
                    round_version=scheduled.round_version,
                    option_id=option_id,
                    client_answer_id=client_answer_id,
                    client_elapsed_ms=client_elapsed_ms,
                ),
            )
            return SavedAnswer(actor, client_answer_id, receipt, client_elapsed_ms)

        with ThreadPoolExecutor(max_workers=self._count) as executor:
            saved_answers = list(executor.map(submit, planned))
            statuses = list(executor.map(self._answer_status, saved_answers))
        for saved, status, (_, planned_option_id) in zip(
            saved_answers,
            statuses,
            planned,
            strict=True,
        ):
            validate_saved_answer(
                saved.receipt,
                status,
                round_id=scheduled.round_id,
                option_id=planned_option_id,
                client_answer_id=saved.client_answer_id,
                client_elapsed_ms=saved.client_elapsed_ms,
            )
        if len({saved.receipt.answer_id for saved in saved_answers}) != self._count:
            raise ValidationFailure("Participant answers did not receive distinct identities")

        first = saved_answers[0]
        replay = first.actor.client.request_model(
            f"/api/v1/participant/rounds/{scheduled.round_id}/answers",
            AnswerReceiptResponse,
            method="POST",
            request_model=SubmitAnswerRequest(
                round_version=scheduled.round_version,
                option_id=first.receipt.option_id,
                client_answer_id=first.client_answer_id,
                client_elapsed_ms=first.client_elapsed_ms,
            ),
        )
        if (
            not replay.is_replay
            or replay.answer_id != first.receipt.answer_id
            or replay.round_id != first.receipt.round_id
            or replay.option_id != first.receipt.option_id
            or replay.client_answer_id != first.client_answer_id
            or replay.client_elapsed_ms != first.client_elapsed_ms
            or replay.timing_basis != "client_monotonic_v1"
        ):
            raise ValidationFailure("Saved-answer replay was not idempotent")
        return saved_answers, expected_option_counts(option_ids, selected_option_ids)

    @staticmethod
    def _answer_status(saved: SavedAnswer) -> AcceptedAnswerStatusResponse:
        return saved.actor.client.request_model(
            f"/api/v1/participant/answers/{saved.client_answer_id}/status",
            AcceptedAnswerStatusResponse,
        )

    def _wait_for_statistics(
        self,
        expected_counts: tuple[int, ...],
    ) -> OperatorStateResponse:
        deadline = time.monotonic() + STATE_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = operator_state(self._operator)
            round_ = require_current_round(state)
            if round_.statistics.answer_count == self._count:
                option_ids = [option.option_id for option in round_.question.options]
                validate_statistics(round_.statistics, option_ids, expected_counts)
                return state
            time.sleep(STATE_POLL_SECONDS)
        raise ValidationFailure("Operator statistics did not reach the saved answer count")

    def _publish_aggregated_results(
        self,
        scheduled: OperatorMutationResponse,
        statistics_state: OperatorStateResponse,
    ) -> None:
        if scheduled.standard_close_at is None:
            raise ValidationFailure("Scheduled round close time is missing")
        wait_for_server_time(
            JsonClient(self._base_url),
            scheduled.standard_close_at + timedelta(milliseconds=CLOSE_SAFETY_MARGIN_MS),
        )
        expected_round_id = require_current_round(statistics_state).round_id
        deadline = time.monotonic() + STATE_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            published_round = require_current_round(operator_state(self._operator))
            if published_round.round_id != expected_round_id:
                raise ValidationFailure("Current round changed before publication")
            if published_round.state == "VoidCandidate":
                failure_code = published_round.result_failure_code or "unknown"
                raise ValidationFailure(f"Automatic result aggregation failed ({failure_code})")
            if (
                published_round.state == "ReadyToReveal"
                and published_round.result_publication == "Withheld"
                and published_round.can_publish_results
            ):
                if (
                    published_round.statistics.answer_count != self._count
                    or published_round.statistics.answer_count
                    != sum(option.answer_count for option in published_round.statistics.options)
                ):
                    raise ValidationFailure(
                        "Aggregated result counts do not match saved participants"
                    )
                mutate_round(
                    self._operator,
                    f"/api/v1/operator/rounds/{published_round.round_id}/publish-results",
                    OperatorMutationRequest(
                        expected_version=published_round.version,
                        idempotency_key=uuid4(),
                    ),
                )
                return
            if published_round.result_publication == "Published":
                raise ValidationFailure("Round results were published before the operator action")
            time.sleep(STATE_POLL_SECONDS)
        raise ValidationFailure("Round results were not ready for publication")

    def _validate_published(
        self,
        saved_answers: list[SavedAnswer],
        expected_counts: tuple[int, ...],
        preview: OperatorQuestionPreviewResponse,
    ) -> None:
        published_state = operator_state(self._operator)
        participant_states = [self._session(saved.actor) for saved in saved_answers]
        validate_published_projections(
            published_state,
            participant_states,
            saved_answers,
            expected_counts,
            preview,
        )


def validate(base_url: str, participant_base_url: str, count: int) -> RehearsalResult:
    return DemoRehearsal(base_url, participant_base_url, count).run()


def main() -> int:
    args = parse_args()
    participant_base_url = args.participant_base_url or args.base_url
    logfire.configure(send_to_logfire=False)
    logfire.info(
        "demo acceptance started",
        participant_count=args.participants,
        has_separate_participant_route=participant_base_url != args.base_url,
    )
    try:
        result = validate(args.base_url, participant_base_url, args.participants)
    except (
        HTTPError,
        HTTPException,
        OSError,
        TimeoutError,
        URLError,
        ValidationError,
        ValidationFailure,
        ValueError,
    ) as error:
        logfire.exception(
            "demo acceptance failed",
            participant_count=args.participants,
            error_type=type(error).__name__,
        )
        print(f"Acceptance failed: {type(error).__name__}: {error}")
        return 1

    distribution = "/".join(str(count) for count in result.option_counts)
    print("Acceptance passed:")
    print("- Caddy readiness and participant entry route are ready.")
    print(
        f"- {result.participant_count} independent participants joined and received the question."
    )
    print("- Operator-only question preview and current-question switch were verified.")
    print(f"- All answers were saved concurrently and confirmed ({distribution}).")
    print("- Operator statistics matched saved answers and result visibility matched its policy.")
    print("- The next-question preview stayed out of participant projections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
