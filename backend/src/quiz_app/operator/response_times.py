from functools import lru_cache
from statistics import mean, median
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, or_, select
from sqlmodel import Session

from quiz_app.db import get_engine
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.domain.round_number import is_practice_round, quiz_round_number
from quiz_app.models import Event, Participant, Question, QuizRound, ScoreLedger, ScoringPolicy
from quiz_app.models.enums import RoundResultPublication, RoundState, ScoreExclusionReason
from quiz_app.operator.response_time_contracts import (
    OperatorResponseTimeFilters,
    OperatorResponseTimeRound,
    OperatorResponseTimeRow,
    OperatorResponseTimesResponse,
    OperatorResponseTimeStatistics,
)
from quiz_app.public_label import PublicLabel
from quiz_app.realtime.projection_budget import get_projection_read_budget
from quiz_app.realtime.state_cache import configure_projection_read_timeout


class ResponseTimeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    public_label: PublicLabel
    response_ms: int = Field(ge=0)
    is_correct: bool


class OperatorResponseTimeService:
    """確定した問題の回答時間だけを運営向けに集計する。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_response_times(
        self, event_id: UUID, filters: OperatorResponseTimeFilters
    ) -> OperatorResponseTimesResponse:
        """指定した集計済み問題の母集団から、全回答・正答の統計を返す。"""

        with get_projection_read_budget().read(normal=True), Session(self._engine) as session:
            configure_projection_read_timeout(session)
            event = session.get(Event, event_id)
            if event is None:
                raise ResourceNotFoundError("event_not_found")
            policy = session.get(ScoringPolicy, event.scoring_policy_version)
            if policy is None:
                raise DomainInvariantError("scoring_policy_not_found")
            rounds = self._aggregated_rounds(session, event_id)
            selected_round_id = filters.round_id
            if selected_round_id is None and rounds:
                selected_round_id = rounds[-1].round_id
            if selected_round_id is not None and not any(
                round_.round_id == selected_round_id for round_ in rounds
            ):
                raise ResourceNotFoundError("response_time_round_not_found")
            answers = (
                self._answers(session, event_id, selected_round_id)
                if selected_round_id is not None
                else []
            )
            return OperatorResponseTimesResponse(
                rounds=rounds,
                selected_round_id=selected_round_id,
                timing_basis=policy.timing_basis,
                all_answers=response_time_statistics(answers),
                correct_answers=response_time_statistics(
                    [answer for answer in answers if answer.is_correct]
                ),
            )

    @staticmethod
    def _aggregated_rounds(session: Session, event_id: UUID) -> list[OperatorResponseTimeRound]:
        rows = session.execute(
            select(QuizRound, Question.prompt)
            .join(Question, Question.id == QuizRound.question_id)
            .where(
                QuizRound.event_id == event_id,
                Question.event_id == event_id,
                or_(
                    QuizRound.state == RoundState.READY_TO_REVEAL,
                    QuizRound.result_publication == RoundResultPublication.PUBLISHED,
                ),
                QuizRound.state != RoundState.VOIDED,
                QuizRound.void_reason.is_(None),
            )
            .order_by(QuizRound.version)
        ).all()
        result: list[OperatorResponseTimeRound] = []
        for round_, prompt in rows:
            round_number = quiz_round_number(session, round_)
            result.append(
                OperatorResponseTimeRound(
                    round_id=round_.id,
                    round_number=round_number,
                    is_practice=is_practice_round(round_number),
                    prompt=prompt,
                )
            )
        return result

    @staticmethod
    def _answers(session: Session, event_id: UUID, round_id: UUID) -> list[ResponseTimeAnswer]:
        rows = session.execute(
            select(ScoreLedger, Participant.public_label)
            .join(Participant, Participant.id == ScoreLedger.participant_id)
            .where(ScoreLedger.round_id == round_id, Participant.event_id == event_id)
        ).all()
        answers: list[ResponseTimeAnswer] = []
        for ledger, public_label in rows:
            if ledger.elapsed_ms is None or ledger.elapsed_ms < 0:
                raise DomainInvariantError("answer_time_data_missing")
            if ledger.exclusion_reason is ScoreExclusionReason.VOID:
                raise DomainInvariantError("published_round_contains_void_score")
            answers.append(
                ResponseTimeAnswer(
                    public_label=public_label,
                    response_ms=ledger.elapsed_ms,
                    is_correct=ledger.is_correct,
                )
            )
        return answers


def response_time_statistics(answers: list[ResponseTimeAnswer]) -> OperatorResponseTimeStatistics:
    """採点台帳の正式回答時間で順位を付け、同じミリ秒は競技順位の同順位にする。"""

    ordered = sorted(answers, key=lambda answer: (answer.response_ms, answer.public_label))
    rows: list[OperatorResponseTimeRow] = []
    previous_ms: int | None = None
    rank = 0
    for index, answer in enumerate(ordered, start=1):
        if answer.response_ms != previous_ms:
            rank = index
            previous_ms = answer.response_ms
        rows.append(
            OperatorResponseTimeRow(
                rank=rank,
                public_label=answer.public_label,
                response_ms=answer.response_ms,
                is_correct=answer.is_correct,
            )
        )
    response_times = [answer.response_ms for answer in ordered]
    return OperatorResponseTimeStatistics(
        answer_count=len(ordered),
        average_response_ms=mean(response_times) if response_times else None,
        median_response_ms=median(response_times) if response_times else None,
        fastest_response_ms=response_times[0] if response_times else None,
        slowest_response_ms=response_times[-1] if response_times else None,
        rows=rows,
    )


@lru_cache
def get_operator_response_time_service() -> OperatorResponseTimeService:
    """既存DB engineを共有する運営専用の読取りserviceを取得する。"""

    return OperatorResponseTimeService(get_engine())
