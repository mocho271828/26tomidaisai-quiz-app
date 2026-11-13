from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from time import monotonic
from uuid import UUID

from sqlalchemy import Engine, case, extract, func, select
from sqlalchemy.sql import Select
from sqlmodel import Session

from quiz_app.db import get_engine
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.domain.rounds import (
    resolve_timed_round_state,
    round_state_version,
)
from quiz_app.models import (
    Answer,
    Event,
    Participant,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    ScoringPolicy,
)
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.operator.control_contracts import (
    OperatorLatestRankingResponse,
    OperatorOptionResponse,
    OperatorOptionStatisticsResponse,
    OperatorQuestionPreviewResponse,
    OperatorQuestionResponse,
    OperatorRankingRowResponse,
    OperatorRoundResponse,
    OperatorRoundStatisticsResponse,
    OperatorStateResponse,
)
from quiz_app.quiz_configuration import require_event_configuration, visibility_state
from quiz_app.realtime.projection_budget import get_projection_read_budget
from quiz_app.realtime.state_cache import (
    CommonStateCache,
    ProjectionSnapshot,
    configure_projection_read_timeout,
    projection_validity_seconds,
)
from quiz_app.single_event import resolve_single_event_id


@dataclass(frozen=True, slots=True)
class OperatorRoundSources:
    question: OperatorQuestionResponse
    statistics: OperatorRoundStatisticsResponse


class OperatorProjectionService:
    """運営画面だけに正解を含む状態projectionを構築する。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._common_cache = CommonStateCache[OperatorStateResponse]()

    def invalidate_common_state(self, event_id: UUID) -> None:
        self._common_cache.invalidate(event_id)

    def clear_common_cache(self) -> None:
        self._common_cache.clear()

    async def close_common_cache(self) -> None:
        self._common_cache.close()

    def get_event_state(self, event_id: UUID) -> OperatorStateResponse:
        """正本を変更せず、DB時刻から導出した最新の運営状態を返す。"""

        return self.get_event_snapshot(event_id).state

    def get_event_snapshot(
        self, event_id: UUID, *, normal: bool = False
    ) -> ProjectionSnapshot[OperatorStateResponse]:
        return self._common_cache.get_snapshot(
            event_id,
            lambda: self._load_event_snapshot(event_id, normal=normal),
            lambda value: value.event_version,
        )

    def _load_event_snapshot(
        self, event_id: UUID, *, normal: bool = False
    ) -> ProjectionSnapshot[OperatorStateResponse]:

        with (
            get_projection_read_budget().read(normal=normal, timeout=0 if normal else 5.0),
            Session(self._engine) as session,
        ):
            configure_projection_read_timeout(session)
            if resolve_single_event_id(session) != event_id:
                raise ResourceNotFoundError("event_not_found")
            # 表示設定と結果を同じ公開境界から返すため、全読取りの間Event更新を待たせる。
            event = session.get(Event, event_id, with_for_update={"read": True})
            if event is None:
                raise ResourceNotFoundError("event_not_found")
            participant_count = self._count(
                session,
                select(func.count())
                .select_from(Participant)
                .where(Participant.event_id == event_id),
            )
            round_ = session.scalar(
                select(QuizRound)
                .where(QuizRound.event_id == event_id)
                .order_by(QuizRound.version.desc())
                .limit(1)
            )
            round_sources = self._round_sources(session, round_) if round_ is not None else None
            available_questions = session.scalars(
                select(Question)
                .where(Question.event_id == event_id)
                .order_by(Question.display_order, Question.version)
            ).all()
            used_question_ids = session.scalars(
                select(QuizRound.question_id)
                .where(QuizRound.event_id == event_id)
                .order_by(QuizRound.version)
            ).all()
            configuration = require_event_configuration(session, event)
            question_previews = self._question_previews(session, available_questions)
            ranking_version = self._latest_ranking_snapshot_version(session, event.id)
            sampled_from = monotonic()
            database_time = session.scalar(select(func.clock_timestamp()))
            if not isinstance(database_time, datetime):
                raise DomainInvariantError("database_clock_unavailable")
            current_round = (
                self._round_projection(
                    session,
                    round_,
                    database_time,
                    sources=round_sources,
                    event_in_progress=event.state is EventState.IN_PROGRESS,
                )
                if round_ is not None
                else None
            )
            state = OperatorStateResponse(
                event_id=event.id,
                projected_at=database_time,
                event_title=event.title,
                event_state=event.state,
                award_ceremony_phase=(
                    configuration.award_ceremony_phase
                    if event.state is EventState.COMPLETED
                    else None
                ),
                event_version=event.version,
                start_delay_seconds=configuration.settings.start_delay_seconds,
                result_visibility=visibility_state(configuration.record),
                visibility_controls=configuration.settings.visibility,
                participant_count=participant_count,
                available_questions=question_previews,
                used_question_ids=list(dict.fromkeys(used_question_ids)),
                current_round=current_round,
                latest_ranking_snapshot_version=ranking_version,
            )
            return ProjectionSnapshot(
                state,
                sampled_from,
                sampled_from
                + projection_validity_seconds(
                    database_time,
                    (round_.open_at, round_.standard_close_at) if round_ is not None else (),
                ),
            )

    @staticmethod
    def _question_previews(
        session: Session,
        questions: Sequence[Question],
    ) -> list[OperatorQuestionPreviewResponse]:
        """運営向け候補問題の全選択肢を1 queryで取得する。"""

        if not questions:
            return []
        question_ids = [question.id for question in questions]
        options = session.scalars(
            select(QuestionOption)
            .where(QuestionOption.question_id.in_(question_ids))
            .order_by(QuestionOption.question_id, QuestionOption.display_order)
        ).all()
        options_by_question: dict[UUID, list[QuestionOption]] = {
            question_id: [] for question_id in question_ids
        }
        for option in options:
            question_options = options_by_question.get(option.question_id)
            if question_options is None:
                raise DomainInvariantError("question_option_not_found_for_available_question")
            question_options.append(option)

        previews: list[OperatorQuestionPreviewResponse] = []
        for question in questions:
            question_options = options_by_question[question.id]
            if not question_options:
                raise DomainInvariantError("question_options_not_found")
            previews.append(
                OperatorQuestionPreviewResponse(
                    question_id=question.id,
                    version=question.version,
                    display_order=question.display_order,
                    prompt=question.prompt,
                    options=[
                        OperatorOptionResponse(
                            option_id=option.id,
                            label=option.label,
                            content=option.content,
                            is_correct=option.is_correct,
                        )
                        for option in question_options
                    ],
                )
            )
        return previews

    def get_latest_ranking(
        self,
        event_id: UUID,
    ) -> OperatorLatestRankingResponse | None:
        """イベントの最新順位snapshotを運営画面向けに返す。"""

        with get_projection_read_budget().read(normal=True), Session(self._engine) as session:
            configure_projection_read_timeout(session)
            event = session.get(Event, event_id)
            if event is None:
                raise ResourceNotFoundError("event_not_found")
            return self._latest_ranking(session, event.id)

    def _round_projection(
        self,
        session: Session,
        round_: QuizRound,
        database_time: datetime,
        *,
        sources: OperatorRoundSources | None = None,
        event_in_progress: bool = True,
    ) -> OperatorRoundResponse:
        if sources is None:
            sources = self._round_sources(session, round_)
        projected_state = resolve_timed_round_state(round_, database_time)
        return OperatorRoundResponse(
            round_id=round_.id,
            version=round_.version,
            state_version=round_state_version(round_.version, projected_state),
            state=projected_state,
            open_at=round_.open_at,
            standard_close_at=round_.standard_close_at,
            can_publish_results=(
                event_in_progress
                and projected_state is RoundState.READY_TO_REVEAL
                and round_.result_publication is RoundResultPublication.WITHHELD
            ),
            result_publication=round_.result_publication,
            result_failure_code=(
                round_.void_reason if projected_state is RoundState.VOID_CANDIDATE else None
            ),
            statistics=sources.statistics,
            question=sources.question,
        )

    def _round_sources(self, session: Session, round_: QuizRound) -> OperatorRoundSources:
        question = session.get(Question, round_.question_id)
        if question is None:
            raise DomainInvariantError("round_question_not_found")
        options = session.scalars(
            select(QuestionOption)
            .where(QuestionOption.question_id == question.id)
            .order_by(QuestionOption.display_order)
        ).all()
        if not options:
            raise DomainInvariantError("question_options_not_found")

        statistics = self._answer_statistics(session, round_, options)
        return OperatorRoundSources(
            statistics=statistics,
            question=OperatorQuestionResponse(
                question_id=question.id,
                version=question.version,
                prompt=question.prompt,
                options=[
                    OperatorOptionResponse(
                        option_id=option.id,
                        label=option.label,
                        content=option.content,
                        is_correct=option.is_correct,
                    )
                    for option in options
                ],
            ),
        )

    @staticmethod
    def _answer_statistics(
        session: Session,
        round_: QuizRound,
        options: Sequence[QuestionOption],
    ) -> OperatorRoundStatisticsResponse:
        if round_.open_at is None:
            answer_count = OperatorProjectionService._count(
                session,
                select(func.count()).select_from(Answer).where(Answer.round_id == round_.id),
            )
            if answer_count > 0:
                raise DomainInvariantError("answer_time_data_missing")
            return OperatorProjectionService._statistics_from_option_aggregates(
                options,
                {},
            )

        elapsed_ms = case(
            (ScoringPolicy.timing_basis == "client_monotonic_v1", Answer.client_elapsed_ms),
            else_=func.floor(extract("epoch", Answer.received_at - round_.open_at) * 1_000),
        )
        rows = session.execute(
            select(
                Answer.option_id,
                func.count(Answer.id),
                func.sum(elapsed_ms),
                func.count(elapsed_ms),
            )
            .select_from(Answer)
            .join(QuizRound, QuizRound.id == Answer.round_id)
            .join(Event, Event.id == QuizRound.event_id)
            .join(ScoringPolicy, ScoringPolicy.version == Event.scoring_policy_version)
            .where(Answer.round_id == round_.id)
            .group_by(Answer.option_id)
        ).all()
        known_option_ids = {option.id for option in options}
        option_aggregates: dict[UUID, tuple[int, int]] = {}
        for option_id, raw_answer_count, raw_elapsed_ms_total, timed_answer_count in rows:
            if option_id not in known_option_ids:
                raise DomainInvariantError("answer_option_not_found_for_question")
            if raw_elapsed_ms_total is None or timed_answer_count != raw_answer_count:
                raise DomainInvariantError("answer_time_data_missing")
            option_aggregates[option_id] = (
                int(raw_answer_count),
                int(raw_elapsed_ms_total),
            )
        return OperatorProjectionService._statistics_from_option_aggregates(
            options,
            option_aggregates,
        )

    @staticmethod
    def _statistics_from_option_aggregates(
        options: Sequence[QuestionOption],
        option_aggregates: dict[UUID, tuple[int, int]],
    ) -> OperatorRoundStatisticsResponse:
        known_option_ids = {option.id for option in options}
        if set(option_aggregates) - known_option_ids:
            raise DomainInvariantError("answer_option_not_found_for_question")
        answer_count = sum(count for count, _ in option_aggregates.values())
        elapsed_ms_total = sum(total for _, total in option_aggregates.values())
        option_statistics: list[OperatorOptionStatisticsResponse] = []
        for option in options:
            option_answer_count, option_elapsed_ms_total = option_aggregates.get(
                option.id,
                (0, 0),
            )
            option_statistics.append(
                OperatorOptionStatisticsResponse(
                    option_id=option.id,
                    answer_count=option_answer_count,
                    average_response_ms=OperatorProjectionService._average(
                        option_elapsed_ms_total,
                        option_answer_count,
                    ),
                )
            )

        return OperatorRoundStatisticsResponse(
            answer_count=answer_count,
            average_response_ms=OperatorProjectionService._average(
                elapsed_ms_total,
                answer_count,
            ),
            options=option_statistics,
        )

    @staticmethod
    def _average(elapsed_ms_total: int, answer_count: int) -> float | None:
        if answer_count == 0:
            return None
        return elapsed_ms_total / answer_count

    @staticmethod
    def _latest_ranking_snapshot_version(
        session: Session,
        event_id: UUID,
    ) -> int | None:
        version = session.scalar(
            select(func.max(RankSnapshot.version)).where(RankSnapshot.event_id == event_id)
        )
        return int(version) if version is not None else None

    @staticmethod
    def _latest_ranking(
        session: Session,
        event_id: UUID,
    ) -> OperatorLatestRankingResponse | None:
        snapshot = session.scalar(
            select(RankSnapshot)
            .where(RankSnapshot.event_id == event_id)
            .order_by(RankSnapshot.version.desc())
            .limit(1)
        )
        if snapshot is None:
            return None
        rows = session.scalars(
            select(RankSnapshotRow)
            .where(RankSnapshotRow.snapshot_id == snapshot.id)
            .order_by(RankSnapshotRow.overall_rank, RankSnapshotRow.public_label)
        ).all()
        return OperatorLatestRankingResponse(
            snapshot_version=snapshot.version,
            round_id=snapshot.round_id,
            rows=[
                OperatorRankingRowResponse(
                    overall_rank=row.overall_rank,
                    public_label=row.public_label,
                    total_points=row.total_points,
                    correct_count=row.correct_count,
                )
                for row in rows
            ],
        )

    @staticmethod
    def _count(session: Session, statement: Select[tuple[int]]) -> int:
        count = session.scalar(statement)
        return int(count or 0)


@lru_cache
def get_operator_projection_service() -> OperatorProjectionService:
    engine = get_engine()
    return OperatorProjectionService(engine)
