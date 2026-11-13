from collections.abc import Sequence
from datetime import datetime
from functools import lru_cache
from time import monotonic
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlmodel import Session

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.award_ceremony import award_ceremony_projection
from quiz_app.audience.contracts import (
    AcceptedAnswerStatusResponse,
    AnswerStatusResponse,
    AudienceEventResponse,
    AudienceOptionResponse,
    AudienceQuestionResponse,
    ParticipantAnswerResponse,
    ParticipantCommonLatestResultResponse,
    ParticipantCommonStateResponse,
    ParticipantCurrentRoundResponse,
    ParticipantIdentityResponse,
    ParticipantStateResponse,
    PersonalResultResponse,
    PublishedDistributionResponse,
    PublishedOptionResponse,
    PublishedResultResponse,
    UnsavedAnswerStatusResponse,
)
from quiz_app.audience.result_delivery import PersonalResultService
from quiz_app.db import get_engine
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.domain.round_number import quiz_round_number
from quiz_app.domain.rounds import resolve_timed_round_state, round_state_version
from quiz_app.models import (
    Answer,
    Event,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RoundOptionStat,
    ScoringPolicy,
)
from quiz_app.models.enums import RoundResultPublication
from quiz_app.observability.answer_diagnostics import record_answer_event
from quiz_app.observability.request_timing import observe_stage
from quiz_app.quiz_configuration import (
    QuizSettings,
    require_event_configuration,
    visibility_state,
)
from quiz_app.realtime.projection_budget import get_projection_read_budget
from quiz_app.realtime.state_cache import (
    CommonStateCache,
    CommonStatePreparation,
    CommonStateRefreshRequired,
    ProjectionSnapshot,
    configure_projection_read_timeout,
    projection_validity_seconds,
)
from quiz_app.single_event import resolve_single_event_id


class AudienceProjectionService:
    """参加者への情報公開範囲を固定する。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._common_cache = CommonStateCache[ParticipantCommonStateResponse]()
        self._common_preparation = CommonStatePreparation(self.get_common_state)
        self.personal_results = PersonalResultService(engine)

    def invalidate_common_state(self, event_id: UUID) -> None:
        self._common_cache.invalidate(event_id)
        self.personal_results.invalidate(event_id)

    def clear_common_cache(self) -> None:
        self._common_cache.clear()

    async def prepare_common_state(self, event_id: UUID) -> ParticipantCommonStateResponse:
        """HTTP row lock取得前に、process内で共有する構築を有限時間だけ待つ。"""

        return await self._common_preparation.prepare(event_id)

    async def close_common_cache(self) -> None:
        self._common_cache.close()
        await self._common_preparation.close()
        await self.personal_results.close()

    def get_common_state(self, event_id: UUID) -> ParticipantCommonStateResponse:
        """WS hubは参加者ごとにDBを取得せず、同じsnapshotを使う。"""

        return self.get_common_snapshot(event_id).state

    def get_common_snapshot(
        self, event_id: UUID, *, normal: bool = False
    ) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        def load() -> ProjectionSnapshot[ParticipantCommonStateResponse]:
            with (
                get_projection_read_budget().read(normal=normal, timeout=0 if normal else 5.0),
                Session(self._engine) as session,
            ):
                configure_projection_read_timeout(session)
                if resolve_single_event_id(session) != event_id:
                    raise ResourceNotFoundError("event_not_found")
                event = session.get(Event, event_id, with_for_update={"read": True})
                if event is None:
                    raise ResourceNotFoundError("event_not_found")
                return self._build_common_snapshot(session, event)

        return self._common_cache.get_snapshot(
            event_id, load, lambda value: value.event.event_version
        )

    def _build_common_snapshot(
        self, session: Session, event: Event
    ) -> ProjectionSnapshot[ParticipantCommonStateResponse]:
        configuration = require_event_configuration(session, event)
        visibility = visibility_state(configuration.record)
        latest_round = self._latest_round(session, event.id)
        current_round = self._current_round(latest_round)
        latest_result_round = self._latest_result_round(session, event.id, latest_round)
        current_question = None
        current_number = None
        if current_round is not None:
            current_question, _ = self._question_projection(session, current_round)
            current_number = quiz_round_number(session, current_round)
        result_question = None
        public_result = None
        result_number = None
        if latest_result_round is not None:
            result_question, options = self._question_projection(session, latest_result_round)
            public_result = self._published_result(
                session, latest_result_round, options, visibility.distribution
            )
            result_number = quiz_round_number(session, latest_result_round)
        snapshot = self._latest_snapshot(session, event.id) if visibility.personal_ranking else None
        ceremony = award_ceremony_projection(session, event, configuration.award_ceremony_phase)
        # DB時計より前にSQL入力を揃え、接続取得・lock・query待ちを標本の年齢へ加えない。
        sampled_from = monotonic()
        projected_at = self._database_time(session)
        latest_result = None
        if latest_result_round is not None and result_number is not None:
            latest_result = self._common_latest_result_projection(
                session,
                latest_result_round,
                projected_at,
                round_number=result_number,
                is_distribution_visible=visibility.distribution,
                question=result_question,
                published_result=public_result,
            )
        state = ParticipantCommonStateResponse(
            projected_at=projected_at,
            event=self._event_response(event, configuration.settings),
            award_ceremony=ceremony,
            current_round=(
                self._current_round_projection(
                    session,
                    current_round,
                    projected_at,
                    round_number=current_number,
                    question=current_question,
                )
                if current_round is not None and current_number is not None
                else None
            ),
            latest_result=latest_result,
            result_visibility=visibility,
            latest_ranking_snapshot_version=snapshot.version if snapshot is not None else None,
        )
        # Event共有lock内の観測だけが、通知による無効化を解除できる。
        self.personal_results.observe(state)
        state = state.model_copy(
            update={
                "personal_result_delivery": self.personal_results.delivery(state),
            }
        )
        return ProjectionSnapshot(
            state,
            sampled_from,
            sampled_from
            + projection_validity_seconds(
                projected_at,
                (
                    current_round.open_at,
                    current_round.standard_close_at,
                )
                if current_round is not None
                else (),
            ),
        )

    def get_participant_state(
        self,
        principal: ParticipantPrincipal,
    ) -> ParticipantStateResponse:
        for _attempt in range(3):
            self.get_common_state(principal.event_id)
            try:
                with Session(self._engine) as session:
                    return self.get_participant_state_in_session(session, principal)
            except CommonStateRefreshRequired:
                continue
        raise DomainInvariantError("common_state_changed_during_projection")

    def get_participant_state_in_session(
        self,
        session: Session,
        principal: ParticipantPrincipal,
    ) -> ParticipantStateResponse:
        """呼出し元のtransactionで公開境界を固定し、本人projectionを構築する。"""

        # 公開境界のcommitが読取り途中へ割り込んで、旧表示設定と新結果が混ざるのを防ぐ。
        session.connection()
        with observe_stage("db_lock_query"):
            event = session.get(Event, principal.event_id, with_for_update={"read": True})
        if event is None:
            raise ResourceNotFoundError("event_not_found")
        common = self._common_cache.peek(event.id, event.version)
        if common is None:
            # Event共有lockを保持したまま他threadのbuilderを待つとlock順序が逆転する。
            # async run_syncでもCondition待機をせず、Session退出後にsingle-flightへ戻す。
            raise CommonStateRefreshRequired(event.id)
        answer = self._participant_answer_for_round(
            session,
            principal.participant_id,
            common.current_round.round_id if common.current_round is not None else None,
        )
        return ParticipantStateResponse(
            projected_at=self._database_time(session),
            event=common.event,
            award_ceremony=common.award_ceremony,
            participant=ParticipantIdentityResponse(
                public_label=principal.public_label,
                is_ranking_eligible=True,
            ),
            current_round=common.current_round,
            latest_result=common.latest_result,
            answer=answer,
            result_visibility=common.result_visibility,
            latest_ranking_snapshot_version=common.latest_ranking_snapshot_version,
            personal_result_delivery=self.personal_results.delivery(common),
        )

    def get_participant_result_in_session(
        self, session: Session, principal: ParticipantPrincipal
    ) -> PersonalResultResponse:
        """公開境界だけをDBで固定し、本人結果は共有batchから抽出する。"""

        configure_projection_read_timeout(session)
        event = session.get(Event, principal.event_id, with_for_update={"read": True})
        if event is None:
            raise ResourceNotFoundError("event_not_found")
        common = self._common_cache.peek(event.id, event.version)
        if common is None:
            raise CommonStateRefreshRequired(event.id)
        return self.personal_results.result_for(principal, common)

    def get_server_time(self) -> datetime:
        with Session(self._engine) as session:
            return self.get_server_time_in_session(session)

    def get_server_time_in_session(self, session: Session) -> datetime:
        """同じrequestで取得済みのSessionから正本時計を読む。"""

        return self._database_time(session)

    def get_answer_status(
        self,
        principal: ParticipantPrincipal,
        client_answer_id: UUID,
    ) -> AnswerStatusResponse:
        """本人sessionに属するclient answer IDだけをread-onlyで照会する。"""

        with Session(self._engine) as session:
            return self.get_answer_status_in_session(session, principal, client_answer_id)

    def get_answer_status_in_session(
        self,
        session: Session,
        principal: ParticipantPrincipal,
        client_answer_id: UUID,
    ) -> AnswerStatusResponse:
        """認証と共通のSessionで本人の回答だけを照会する。"""

        return self._answer_status(session, principal.participant_id, client_answer_id)

    def _current_round_projection(
        self,
        session: Session,
        round_: QuizRound,
        projected_at: datetime,
        *,
        round_number: int,
        question: AudienceQuestionResponse | None = None,
    ) -> ParticipantCurrentRoundResponse:
        if round_.result_publication is not RoundResultPublication.WITHHELD:
            raise DomainInvariantError("current_round_must_be_withheld")
        projected_state = resolve_timed_round_state(round_, projected_at)
        timeline = {
            "round_id": round_.id,
            "round_number": round_number,
            "version": round_.version,
            "state_version": round_state_version(round_.version, projected_state),
            "state": projected_state,
            "open_at": round_.open_at,
            "standard_close_at": round_.standard_close_at,
        }
        question_response = question
        if question_response is None:
            question_response, _ = self._question_projection(session, round_)
        return ParticipantCurrentRoundResponse(
            **timeline,
            question=question_response,
        )

    def _common_latest_result_projection(
        self,
        session: Session,
        round_: QuizRound,
        projected_at: datetime,
        *,
        round_number: int,
        is_distribution_visible: bool,
        question: AudienceQuestionResponse | None = None,
        published_result: PublishedResultResponse | None = None,
    ) -> ParticipantCommonLatestResultResponse:
        if round_.result_publication is not RoundResultPublication.PUBLISHED:
            raise DomainInvariantError("latest_result_round_must_be_published")
        projected_state = resolve_timed_round_state(round_, projected_at)
        timeline = {
            "round_id": round_.id,
            "round_number": round_number,
            "version": round_.version,
            "state_version": round_state_version(round_.version, projected_state),
            "state": projected_state,
            "open_at": round_.open_at,
            "standard_close_at": round_.standard_close_at,
        }
        question_response = question
        if question_response is None or published_result is None:
            question_response, options = self._question_projection(session, round_)
            published_result = self._published_result(
                session, round_, options, is_distribution_visible
            )
        return ParticipantCommonLatestResultResponse(
            **timeline,
            question=question_response,
            result=published_result,
        )

    @staticmethod
    def _question_projection(
        session: Session,
        round_: QuizRound,
    ) -> tuple[AudienceQuestionResponse, Sequence[QuestionOption]]:
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
        return (
            AudienceQuestionResponse(
                question_id=question.id,
                prompt=question.prompt,
                options=[
                    AudienceOptionResponse(
                        option_id=option.id,
                        label=option.label,
                        content=option.content,
                    )
                    for option in options
                ],
            ),
            options,
        )

    @staticmethod
    def _published_result(
        session: Session,
        round_: QuizRound,
        options: Sequence[QuestionOption],
        is_distribution_visible: bool,
    ) -> PublishedResultResponse:
        correct_options = [option for option in options if option.is_correct]
        if len(correct_options) != 1:
            raise DomainInvariantError("question_must_have_exactly_one_correct_option")
        if not is_distribution_visible:
            return PublishedResultResponse(
                publication="Published",
                correct_option_id=correct_options[0].id,
                distribution=None,
            )
        stats = session.scalars(
            select(RoundOptionStat).where(RoundOptionStat.round_id == round_.id)
        ).all()
        stat_by_option = {stat.option_id: stat for stat in stats}
        if set(stat_by_option) != {option.id for option in options}:
            raise DomainInvariantError("published_option_stats_incomplete")
        return PublishedResultResponse(
            publication="Published",
            correct_option_id=correct_options[0].id,
            distribution=PublishedDistributionResponse(
                valid_answer_count=sum(stat.valid_response_count for stat in stats),
                options=[
                    PublishedOptionResponse(
                        option_id=option.id,
                        label=option.label,
                        content=option.content,
                        is_correct=option.is_correct,
                        valid_response_count=stat_by_option[option.id].valid_response_count,
                        response_percentage=stat_by_option[option.id].response_percentage,
                    )
                    for option in options
                ],
            ),
        )

    @staticmethod
    def _participant_answer(
        session: Session,
        participant_id: UUID,
        round_: QuizRound | None,
    ) -> ParticipantAnswerResponse | None:
        return AudienceProjectionService._participant_answer_for_round(
            session, participant_id, round_.id if round_ is not None else None
        )

    @staticmethod
    def _participant_answer_for_round(
        session: Session, participant_id: UUID, round_id: UUID | None
    ) -> ParticipantAnswerResponse | None:
        if round_id is None:
            return None
        row = session.execute(
            select(Answer, ScoringPolicy)
            .join(QuizRound, QuizRound.id == Answer.round_id)
            .join(Event, Event.id == QuizRound.event_id)
            .join(ScoringPolicy, ScoringPolicy.version == Event.scoring_policy_version)
            .where(
                Answer.participant_id == participant_id,
                Answer.round_id == round_id,
            )
        ).one_or_none()
        if row is None:
            return None
        return AudienceProjectionService._answer_response(*row)

    @staticmethod
    def _participant_selected_option_id(
        session: Session,
        participant_id: UUID,
        round_id: UUID,
    ) -> UUID | None:
        return session.scalar(
            select(Answer.option_id).where(
                Answer.participant_id == participant_id,
                Answer.round_id == round_id,
            )
        )

    @staticmethod
    def _answer_status(
        session: Session,
        participant_id: UUID,
        client_answer_id: UUID,
    ) -> AnswerStatusResponse:
        record_answer_event("status_query_started")
        row = session.execute(
            select(Answer, ScoringPolicy)
            .join(QuizRound, QuizRound.id == Answer.round_id)
            .join(Event, Event.id == QuizRound.event_id)
            .join(ScoringPolicy, ScoringPolicy.version == Event.scoring_policy_version)
            .where(
                Answer.participant_id == participant_id,
                Answer.client_answer_id == client_answer_id,
            )
        ).one_or_none()
        if row is None:
            record_answer_event("status_query_finished", status="NotStored")
            return UnsavedAnswerStatusResponse(
                status="NotStored",
                client_answer_id=client_answer_id,
            )
        record_answer_event("status_query_finished", status="AcceptedLocked")
        return AcceptedAnswerStatusResponse(
            status="AcceptedLocked",
            answer=AudienceProjectionService._answer_response(*row),
        )

    @staticmethod
    def _answer_response(answer: Answer, policy: ScoringPolicy) -> ParticipantAnswerResponse:
        if answer.received_at is None:
            raise DomainInvariantError("database_received_at_missing")
        return ParticipantAnswerResponse(
            answer_id=answer.id,
            round_id=answer.round_id,
            option_id=answer.option_id,
            client_answer_id=answer.client_answer_id,
            received_at=answer.received_at,
            client_elapsed_ms=answer.client_elapsed_ms,
            timing_basis=policy.timing_basis,
        )

    @staticmethod
    def _latest_snapshot(session: Session, event_id: UUID) -> RankSnapshot | None:
        snapshot = session.scalar(
            select(RankSnapshot)
            .join(QuizRound, QuizRound.id == RankSnapshot.round_id)
            .where(
                RankSnapshot.event_id == event_id,
                QuizRound.result_publication == RoundResultPublication.PUBLISHED,
            )
            .order_by(RankSnapshot.version.desc())
            .limit(1)
        )
        return snapshot

    @staticmethod
    def _latest_round(session: Session, event_id: UUID) -> QuizRound | None:
        return session.scalar(
            select(QuizRound)
            .where(QuizRound.event_id == event_id)
            .order_by(QuizRound.version.desc())
            .limit(1)
        )

    @staticmethod
    def _current_round(latest_round: QuizRound | None) -> QuizRound | None:
        if (
            latest_round is None
            or latest_round.result_publication is RoundResultPublication.PUBLISHED
        ):
            return None
        return latest_round

    @staticmethod
    def _latest_result_round(
        session: Session,
        event_id: UUID,
        latest_round: QuizRound | None,
    ) -> QuizRound | None:
        if (
            latest_round is not None
            and latest_round.result_publication is RoundResultPublication.PUBLISHED
        ):
            return latest_round
        return session.scalar(
            select(QuizRound)
            .where(
                QuizRound.event_id == event_id,
                QuizRound.result_publication == RoundResultPublication.PUBLISHED,
            )
            .order_by(QuizRound.version.desc())
            .limit(1)
        )

    @staticmethod
    def _event_response(event: Event, settings: QuizSettings) -> AudienceEventResponse:
        return AudienceEventResponse(
            event_id=event.id,
            event_title=event.title,
            event_state=event.state,
            event_version=event.version,
            start_delay_seconds=settings.start_delay_seconds,
            timing_basis=settings.timing.basis,
            arrival_grace_ms=settings.timing.arrival_grace_ms,
            clock_tolerance_ms=settings.timing.clock_tolerance_ms,
            client_ahead_tolerance_ms=settings.timing.client_ahead_tolerance_ms,
        )

    @staticmethod
    def _database_time(session: Session) -> datetime:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise DomainInvariantError("database_clock_unavailable")
        return database_time


@lru_cache
def get_audience_projection_service() -> AudienceProjectionService:
    return AudienceProjectionService(get_engine())
