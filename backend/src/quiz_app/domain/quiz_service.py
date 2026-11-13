from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Engine, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from quiz_app.answer_timing import AnswerTiming, AnswerTimingBasis
from quiz_app.domain.errors import (
    AnswerContentConflictError,
    DomainInvariantError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.domain.round_number import is_practice_round, quiz_round_number
from quiz_app.domain.rounds import (
    STANDARD_DURATION_SECONDS,
    resolve_timed_round_state,
    validate_answer_window,
    validate_publication_time,
)
from quiz_app.domain.scoring import assign_speed_ranks, rank_overall, score_answer
from quiz_app.domain.types import (
    AcceptedAnswer,
    AdvancedRound,
    AggregatedRound,
    AnswerScoreInput,
    CompletedEvent,
    OverallStanding,
    OverallStandingInput,
    PreparationStarted,
    PublishedRound,
    RecoveredRoundResults,
    RoundBackupTransition,
    RoundClockState,
    ScheduledRound,
    ScoringRule,
    SelectedRoundQuestion,
    StandingAccumulator,
    SubmitAnswerCommand,
    VisibilityChanged,
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
    RoundOptionStat,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.observability.answer_diagnostics import record_answer_event, record_database_receipt
from quiz_app.observability.request_timing import observe_stage, record_answer_difference
from quiz_app.quiz_configuration import (
    VisibilityMutationTarget,
    apply_automatic_visibility,
    get_quiz_settings,
    require_event_configuration,
    require_matching_scoring_policy,
    set_manual_visibility,
    visibility_state,
)


class QuizDomainService:
    """PostgreSQL transaction内でquizの正本更新を行う。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def select_round_question_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
        question_id: UUID,
    ) -> SelectedRoundQuestion:
        """配信開始前の問題切替を同じtransactionで確定する。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if round_.state is not RoundState.PREPARING_ASSETS:
            raise InvalidStateTransitionError("round_is_not_preparing_assets")
        if round_.question_id == question_id:
            raise InvalidStateTransitionError("question_is_already_selected")

        # 新Roundの準備中に切替APIを直接呼ばれても、過去Roundの問題を再利用させない。
        self._require_unused_question(session, round_.event_id, question_id)

        round_.question_id = question_id
        round_.version += 1
        database_time = self._database_time(session)
        event = self._event_for_update(session, round_.event_id)
        self._bump_event(event, database_time)
        return SelectedRoundQuestion(
            event_id=event.id,
            event_version=event.version,
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
            question_id=question_id,
        )

    def begin_preparation(self, round_id: UUID, expected_version: int) -> PreparationStarted:
        """運営の配信開始で問題を確定し、明示的な開始予約を可能にする。"""

        with Session(self._engine) as session, session.begin():
            return self.begin_preparation_in_transaction(session, round_id, expected_version)

    def begin_preparation_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> PreparationStarted:
        """callerが所有するtransactionへ準備開始mutationを参加させる。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if round_.state is not RoundState.PREPARING_ASSETS:
            raise InvalidStateTransitionError("round_is_not_preparing_assets")

        database_time = self._database_time(session)
        # 配信対象の集合やACKを保存せず、全登録者が同じ問題を取得する。
        round_.state = RoundState.READY_TO_SCHEDULE
        round_.version += 1
        event = self._event_for_update(session, round_.event_id)
        self._bump_event(event, database_time)
        return PreparationStarted(
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
        )

    def schedule_round(
        self,
        round_id: UUID,
        expected_version: int,
        *,
        target_open_at: datetime | None = None,
    ) -> ScheduledRound:
        """運営判断で、検証済み指定時刻またはDB時刻と設定待機秒から予約する。"""

        with Session(self._engine) as session, session.begin():
            return self.schedule_round_in_transaction(
                session,
                round_id,
                expected_version,
                target_open_at=target_open_at,
            )

    def schedule_round_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
        *,
        target_open_at: datetime | None = None,
    ) -> ScheduledRound:
        """callerが所有するtransactionへ開始予約mutationを参加させる。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if round_.state is not RoundState.READY_TO_SCHEDULE:
            raise InvalidStateTransitionError("round_is_not_ready_to_schedule")
        event = self._event_for_update(session, round_.event_id)
        configuration = require_event_configuration(session, event)
        delay_seconds = configuration.settings.start_delay_seconds
        database_time = self._database_time(session)
        if target_open_at is None:
            open_at = database_time + timedelta(seconds=delay_seconds)
        else:
            minimum_open_at = database_time + timedelta(seconds=max(1, delay_seconds - 1))
            maximum_open_at = database_time + timedelta(seconds=delay_seconds + 1)
            if not minimum_open_at <= target_open_at <= maximum_open_at:
                raise InvalidStateTransitionError("round_target_open_at_out_of_range")
            open_at = target_open_at
        standard_close_at = open_at + timedelta(seconds=STANDARD_DURATION_SECONDS)
        round_.open_at = open_at
        round_.standard_close_at = standard_close_at
        round_.state = RoundState.SCHEDULED
        round_.version += 1

        event.state = EventState.IN_PROGRESS
        self._bump_event(event, database_time)
        return ScheduledRound(
            round_id=round_.id,
            version=round_.version,
            open_at=open_at,
            standard_close_at=standard_close_at,
        )

    def advance_round_clock(self, round_id: UUID) -> RoundClockState:
        """projection取得時にDB時刻からpersist済みRound状態を前進させる。"""

        with Session(self._engine) as session, session.begin():
            round_ = self._round_for_update(session, round_id)
            database_time = self._database_time(session)
            resolved_state = resolve_timed_round_state(round_, database_time)
            event = self._event_for_update(session, round_.event_id)
            if resolved_state is not round_.state:
                round_.state = resolved_state
                round_.version += 1
                self._bump_event(event, database_time)
            return RoundClockState(
                round_id=round_.id,
                round_version=round_.version,
                round_state=round_.state,
                event_version=event.version,
            )

    def submit_answer(self, command: SubmitAnswerCommand) -> AcceptedAnswer:
        """共有Round lock下で1回答を保存し、DB受付時刻で締切を確定する。"""

        try:
            with Session(self._engine) as session, session.begin():
                return self.submit_answer_in_transaction(session, command)
        except IntegrityError:
            with Session(self._engine) as retry_session:
                replay = self.get_answer_replay_in_session(retry_session, command)
                if replay is None:
                    raise
                return replay

    def submit_answer_in_transaction(
        self,
        session: Session,
        command: SubmitAnswerCommand,
        *,
        expected_event_id: UUID | None = None,
    ) -> AcceptedAnswer:
        """保存と締切判定を行い、commit・rollbackは呼出し元へ委ねる。"""

        round_, participant, option, event, policy = self._answer_context_for_share(
            session, command
        )
        record_answer_event("round_locked")
        if expected_event_id is not None and round_.event_id != expected_event_id:
            raise ResourceNotFoundError("round_not_found")
        replay = self.get_answer_replay_in_session(session, command)
        if replay is not None:
            record_answer_event("answer_replay", is_replay=True)
            return replay
        self._require_version(round_, command.round_version)
        record_answer_event("version_checked")

        if participant is None or participant.event_id != round_.event_id:
            raise ResourceNotFoundError("participant_not_found")
        if option is None or option.question_id != round_.question_id:
            raise ResourceNotFoundError("option_not_found_for_round")
        if (
            event.state is not EventState.IN_PROGRESS
            or round_.result_publication is not RoundResultPublication.WITHHELD
            or round_.state
            not in {
                RoundState.SCHEDULED,
                RoundState.OPEN,
                RoundState.AWAITING_VALIDATION,
            }
        ):
            raise InvalidStateTransitionError("round_is_not_accepting_answers")
        # 保存済み再送は上で返し、新規回答だけをこの配置が採点できるpolicyへ限定する。
        require_matching_scoring_policy(policy, get_quiz_settings())
        timing = self._answer_timing(policy)

        answer = Answer(
            participant_id=command.participant_id,
            round_id=command.round_id,
            option_id=command.option_id,
            client_answer_id=command.client_answer_id,
            client_elapsed_ms=(
                command.client_elapsed_ms if timing.basis == "client_monotonic_v1" else None
            ),
        )
        session.add(answer)
        session.flush()
        # PostgreSQLのINSERT RETURNINGがDB受付stampを返すため再SELECTしない。
        if answer.received_at is None:
            raise DomainInvariantError("database_received_at_missing")
        selection_close_at = round_.standard_close_at
        record_database_receipt(
            answer.received_at,
            round_.open_at,
            (
                selection_close_at + timedelta(milliseconds=timing.arrival_grace_ms)
                if selection_close_at is not None
                else None
            ),
        )
        if timing.basis == "client_monotonic_v1" and round_.open_at is not None:
            received_elapsed_ms = (answer.received_at - round_.open_at) // timedelta(milliseconds=1)
            record_answer_difference(received_elapsed_ms - command.client_elapsed_ms)
        validate_answer_window(round_, answer.received_at, timing, command.client_elapsed_ms)
        record_answer_event("window_validated")
        if participant.first_answer_at is None:
            participant.first_answer_at = answer.received_at
        return self._accepted_from_existing(answer, command, timing, is_replay=False)

    def get_answer_replay_in_session(
        self,
        session: Session,
        command: SubmitAnswerCommand,
    ) -> AcceptedAnswer | None:
        """失敗したtransactionの外で再送を照会し、異なる内容は拒否する。"""

        existing = self._find_existing_answer(session, command)
        if existing is None:
            return None
        answer, policy = existing
        return self._accepted_from_existing(
            answer, command, self._answer_timing(policy), is_replay=True
        )

    def aggregate_round_results_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> AggregatedRound:
        """受付終端後の回答を一度だけ集計し、未公開の確定結果を保存する。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        database_time = self._database_time(session)
        timing = self._round_answer_timing(session, round_)
        validate_publication_time(round_, database_time, timing)
        resolved_state = resolve_timed_round_state(round_, database_time)
        if resolved_state is not RoundState.AWAITING_VALIDATION:
            raise InvalidStateTransitionError("round_is_not_awaiting_validation")
        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")
        if round_.result_publication is not RoundResultPublication.WITHHELD:
            raise InvalidStateTransitionError("round_results_are_already_published")
        require_event_configuration(session, event)
        options = session.scalars(
            select(QuestionOption).where(QuestionOption.question_id == round_.question_id)
        ).all()
        correct_options = [option for option in options if option.is_correct]
        if len(correct_options) != 1:
            raise DomainInvariantError("question_must_have_exactly_one_correct_option")
        correct_option_id = correct_options[0].id

        answer_rows = session.execute(
            select(Answer, Participant)
            .join(Participant, Participant.id == Answer.participant_id)
            .where(Answer.round_id == round_id)
        ).all()
        rule = self._scoring_rule(session, event.scoring_policy_version)
        scored_answers = assign_speed_ranks(
            [
                score_answer(
                    rule,
                    AnswerScoreInput(
                        participant_id=participant.id,
                        public_label=participant.public_label,
                        is_correct=answer.option_id == correct_option_id,
                        elapsed_ms=self._elapsed_ms(round_, answer, timing.basis),
                    ),
                )
                for answer, participant in answer_rows
            ]
        )
        for score in scored_answers:
            session.add(
                ScoreLedger(
                    round_id=round_id,
                    participant_id=score.participant_id,
                    scoring_policy_version=rule.version,
                    is_correct=score.is_correct,
                    base_points=score.base_points,
                    speed_points=score.speed_points,
                    total_points=score.total_points,
                    elapsed_ms=score.elapsed_ms,
                    speed_bucket=score.speed_bucket,
                    speed_rank=score.speed_rank,
                    speed_cohort_size=score.speed_cohort_size,
                    speed_cohort=score.speed_cohort,
                    exclusion_reason=score.exclusion_reason,
                )
            )
        self._store_option_stats(session, round_id, options, answer_rows)
        session.flush()

        standings = self._overall_standings(session, event.id)
        snapshot_version = self._next_snapshot_version(session, event.id)
        snapshot = RankSnapshot(
            event_id=event.id,
            round_id=round_id,
            version=snapshot_version,
        )
        session.add(snapshot)
        for standing in standings:
            session.add(
                RankSnapshotRow(
                    snapshot_id=snapshot.id,
                    public_label=standing.public_label,
                    overall_rank=standing.overall_rank,
                    total_points=standing.total_points,
                    correct_count=standing.correct_count,
                )
            )

        round_.state = RoundState.READY_TO_REVEAL
        round_.version += 1
        self._bump_event(event, database_time)
        return AggregatedRound(
            event_id=event.id,
            round_id=round_.id,
            round_version=round_.version,
            event_version=event.version,
            valid_answer_count=len(answer_rows),
            ranking_row_count=len(standings),
        )

    def publish_round_results_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> PublishedRound:
        """運営の明示操作で保存済み結果だけを公開し、採点・集計は再実行しない。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if (
            round_.state is not RoundState.READY_TO_REVEAL
            or round_.result_publication is not RoundResultPublication.WITHHELD
        ):
            raise InvalidStateTransitionError("round_results_are_not_ready_to_reveal")
        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")
        database_time = self._database_time(session)
        configuration = require_event_configuration(session, event)
        snapshot = session.scalar(select(RankSnapshot).where(RankSnapshot.round_id == round_id))
        if snapshot is None:
            raise DomainInvariantError("aggregated_rank_snapshot_missing")
        valid_answer_count = session.scalar(
            select(func.count()).select_from(ScoreLedger).where(ScoreLedger.round_id == round_id)
        )
        ranking_row_count = session.scalar(
            select(func.count())
            .select_from(RankSnapshotRow)
            .where(RankSnapshotRow.snapshot_id == snapshot.id)
        )
        apply_automatic_visibility(
            configuration.record, configuration.settings, quiz_round_number(session, round_)
        )
        round_.state = RoundState.REVEAL
        round_.result_publication = RoundResultPublication.PUBLISHED
        round_.revealed_at = database_time
        round_.version += 1
        self._bump_event(event, database_time)
        return PublishedRound(
            event_id=event.id,
            round_id=round_.id,
            round_version=round_.version,
            event_version=event.version,
            valid_answer_count=int(valid_answer_count or 0),
            ranking_row_count=int(ranking_row_count or 0),
        )

    def mark_round_results_void_candidate_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
        failure_reason: str,
    ) -> RecoveredRoundResults:
        """集計の永続的失敗を未公開の要確認状態として確定する。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if round_.result_publication is not RoundResultPublication.WITHHELD:
            raise InvalidStateTransitionError("round_results_are_already_published")
        database_time = self._database_time(session)
        validate_publication_time(round_, database_time, self._round_answer_timing(session, round_))
        if resolve_timed_round_state(round_, database_time) is not RoundState.AWAITING_VALIDATION:
            raise InvalidStateTransitionError("round_is_not_awaiting_validation")

        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")
        round_.state = RoundState.VOID_CANDIDATE
        round_.void_reason = failure_reason
        round_.version += 1
        self._bump_event(event, database_time)
        return RecoveredRoundResults(
            event_id=event.id,
            event_version=event.version,
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
        )

    def retry_round_results_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> RecoveredRoundResults:
        """運営確認済みのVoidCandidateを自動集計待ちへ戻す。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if (
            round_.state is not RoundState.VOID_CANDIDATE
            or round_.result_publication is not RoundResultPublication.WITHHELD
        ):
            raise InvalidStateTransitionError("round_is_not_retryable_for_results")
        database_time = self._database_time(session)
        validate_publication_time(round_, database_time, self._round_answer_timing(session, round_))
        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")

        round_.state = RoundState.AWAITING_VALIDATION
        round_.void_reason = None
        round_.version += 1
        self._bump_event(event, database_time)
        return RecoveredRoundResults(
            event_id=event.id,
            event_version=event.version,
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
        )

    def set_result_visibility(
        self,
        event_id: UUID,
        expected_version: int,
        target: VisibilityMutationTarget,
        visible: bool,
    ) -> VisibilityChanged:
        """結果公開とは独立して指定項目の表示状態だけを変更する。"""

        with Session(self._engine) as session, session.begin():
            return self.set_result_visibility_in_transaction(
                session, event_id, expected_version, target, visible
            )

    def set_result_visibility_in_transaction(
        self,
        session: Session,
        event_id: UUID,
        expected_version: int,
        target: VisibilityMutationTarget,
        visible: bool,
    ) -> VisibilityChanged:
        """Event lockとversion照合の下で表示状態を変更する。"""

        event = self._event_for_update(session, event_id)
        if event.version != expected_version:
            raise VersionConflictError("event_version_conflict")
        configuration = require_event_configuration(session, event)
        previous_visibility = visibility_state(configuration.record)
        set_manual_visibility(configuration.record, target, visible)
        current_visibility = visibility_state(configuration.record)
        if previous_visibility != current_visibility:
            self._bump_event(event, self._database_time(session))
        return VisibilityChanged(
            event_id=event.id,
            event_version=event.version,
            visibility=current_visibility,
        )

    def begin_round_backup_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        expected_version: int,
    ) -> RoundBackupTransition:
        """結果公開済みRoundを、backup作成中の排他的な状態へ進める。"""

        round_ = self._round_for_update(session, round_id)
        self._require_version(round_, expected_version)
        if round_.state not in {RoundState.REVEAL, RoundState.ROUND_BACKUP_FAILED}:
            raise InvalidStateTransitionError("round_is_not_ready_for_backup")
        if round_.result_publication is not RoundResultPublication.PUBLISHED:
            raise InvalidStateTransitionError("round_results_are_not_published")
        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")

        if round_.state is RoundState.ROUND_BACKUP_FAILED:
            round_.state = RoundState.ROUND_BACKUP_RETRYING
            round_.version += 1
        round_.state = RoundState.ROUND_BACKUP_PENDING
        round_.version += 1
        session.flush()
        return RoundBackupTransition(
            event_id=event.id,
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
        )

    def validate_next_round_question_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        question_id: UUID,
    ) -> None:
        """backup作成前に次問が同一Eventの未使用問題かを確定する。"""

        round_ = self._round_for_update(session, round_id)
        self._require_unused_question(session, round_.event_id, question_id)

    def fail_round_backup_in_transaction(
        self,
        session: Session,
        round_id: UUID,
    ) -> RoundBackupTransition:
        """backup失敗を永続化し、次Round開始を閉じたままにする。"""

        round_ = self._round_for_update(session, round_id)
        if round_.state is not RoundState.ROUND_BACKUP_PENDING:
            raise InvalidStateTransitionError("round_backup_is_not_pending")
        round_.state = RoundState.ROUND_BACKUP_FAILED
        round_.version += 1
        event = self._event_for_update(session, round_.event_id)
        self._bump_event(event, self._database_time(session))
        return RoundBackupTransition(
            event_id=event.id,
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
        )

    def advance_round_after_backup_in_transaction(
        self,
        session: Session,
        round_id: UUID,
        question_id: UUID,
    ) -> AdvancedRound:
        """検証済みbackupを前提に旧Roundを閉じ、新IDの次Roundを作成する。"""

        round_ = self._round_for_update(session, round_id)
        if round_.state is not RoundState.ROUND_BACKUP_PENDING:
            raise InvalidStateTransitionError("round_backup_is_not_pending")
        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")

        self._require_unused_question(session, event.id, question_id)

        round_.state = RoundState.ROUND_BACKUP_VALIDATED
        round_.version += 1
        # active Round部分indexを解放してから次Roundをinsertする。
        session.flush()
        current_max_version = session.scalar(
            select(func.max(QuizRound.version)).where(QuizRound.event_id == event.id)
        )
        next_round = QuizRound(
            event_id=event.id,
            question_id=question_id,
            version=int(current_max_version or 0) + 1,
            state=RoundState.PREPARING_ASSETS,
            result_publication=RoundResultPublication.WITHHELD,
        )
        session.add(next_round)
        session.flush()
        self._bump_event(event, self._database_time(session))
        return AdvancedRound(
            event_id=event.id,
            event_version=event.version,
            previous_round_id=round_.id,
            new_round_id=next_round.id,
            new_round_version=next_round.version,
            new_round_state=next_round.state,
        )

    def complete_event_after_backup_in_transaction(
        self,
        session: Session,
        round_id: UUID,
    ) -> CompletedEvent:
        """運営が選んだ終了時点の最終backupを確定し、Eventを完了する。"""

        round_ = self._round_for_update(session, round_id)
        if round_.state is not RoundState.ROUND_BACKUP_PENDING:
            raise InvalidStateTransitionError("round_backup_is_not_pending")
        event = self._event_for_update(session, round_.event_id)
        if event.state is not EventState.IN_PROGRESS:
            raise InvalidStateTransitionError("event_is_not_in_progress")

        round_.state = RoundState.ROUND_BACKUP_VALIDATED
        round_.version += 1
        event.state = EventState.COMPLETED
        self._bump_event(event, self._database_time(session))
        return CompletedEvent(
            event_id=event.id,
            event_version=event.version,
            round_id=round_.id,
            round_version=round_.version,
            round_state=round_.state,
        )

    @staticmethod
    def _require_unused_question(
        session: Session,
        event_id: UUID,
        question_id: UUID,
    ) -> None:
        question = session.get(Question, question_id)
        if question is None or question.event_id != event_id:
            raise ResourceNotFoundError("question_not_found_for_event")
        used_question_ids = set(
            session.scalars(
                select(QuizRound.question_id).where(QuizRound.event_id == event_id)
            ).all()
        )
        if question_id in used_question_ids:
            raise InvalidStateTransitionError("question_was_already_used")
        options = session.scalars(
            select(QuestionOption).where(QuestionOption.question_id == question_id)
        ).all()
        if not options or sum(option.is_correct for option in options) != 1:
            raise DomainInvariantError("question_must_have_exactly_one_correct_option")

    @staticmethod
    def _database_time(session: Session) -> datetime:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise DomainInvariantError("database_clock_unavailable")
        return database_time

    @staticmethod
    def _require_version(round_: QuizRound, expected_version: int) -> None:
        if round_.version != expected_version:
            raise VersionConflictError("round_version_conflict")

    @staticmethod
    def _round_for_update(session: Session, round_id: UUID) -> QuizRound:
        round_ = session.scalar(select(QuizRound).where(QuizRound.id == round_id).with_for_update())
        if round_ is None:
            raise ResourceNotFoundError("round_not_found")
        return round_

    @staticmethod
    def _answer_context_for_share(
        session: Session,
        command: SubmitAnswerCommand,
    ) -> tuple[QuizRound, Participant | None, QuestionOption | None, Event, ScoringPolicy | None]:
        session.connection()
        with observe_stage("db_lock_query"):
            row = session.execute(
                select(QuizRound, Participant, QuestionOption, Event, ScoringPolicy)
                .select_from(QuizRound)
                .join(Event, Event.id == QuizRound.event_id)
                .outerjoin(ScoringPolicy, ScoringPolicy.version == Event.scoring_policy_version)
                .outerjoin(Participant, Participant.id == command.participant_id)
                .outerjoin(QuestionOption, QuestionOption.id == command.option_id)
                .where(QuizRound.id == command.round_id)
                .execution_options(populate_existing=True)
                # nullable側をlockせず、運営のRound更新との排他だけを維持する。
                .with_for_update(read=True, of=QuizRound)
            ).one_or_none()
        if row is None:
            raise ResourceNotFoundError("round_not_found")
        return row[0], row[1], row[2], row[3], row[4]

    @staticmethod
    def _event_for_update(session: Session, event_id: UUID) -> Event:
        event = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
        if event is None:
            raise ResourceNotFoundError("event_not_found")
        return event

    @staticmethod
    def _bump_event(event: Event, database_time: datetime) -> None:
        event.version += 1
        event.updated_at = database_time

    @staticmethod
    def _find_existing_answer(
        session: Session,
        command: SubmitAnswerCommand,
    ) -> tuple[Answer, ScoringPolicy | None] | None:
        row = session.execute(
            select(Answer, ScoringPolicy)
            .join(QuizRound, QuizRound.id == Answer.round_id)
            .join(Event, Event.id == QuizRound.event_id)
            .outerjoin(ScoringPolicy, ScoringPolicy.version == Event.scoring_policy_version)
            .where(
                Answer.participant_id == command.participant_id,
                or_(
                    Answer.round_id == command.round_id,
                    Answer.client_answer_id == command.client_answer_id,
                ),
            )
        ).first()
        return None if row is None else (row[0], row[1])

    @staticmethod
    def _accepted_from_existing(
        answer: Answer,
        command: SubmitAnswerCommand,
        timing: AnswerTiming,
        *,
        is_replay: bool,
    ) -> AcceptedAnswer:
        is_same_content = (
            answer.round_id == command.round_id
            and answer.option_id == command.option_id
            and answer.client_answer_id == command.client_answer_id
            and (
                timing.basis == "server_received_v1"
                or answer.client_elapsed_ms == command.client_elapsed_ms
            )
        )
        if not is_same_content:
            raise AnswerContentConflictError("existing_answer_content_conflict")
        if answer.received_at is None:
            raise DomainInvariantError("database_received_at_missing")
        if timing.basis == "client_monotonic_v1" and answer.client_elapsed_ms is None:
            raise DomainInvariantError("client_elapsed_ms_missing")
        return AcceptedAnswer(
            answer_id=answer.id,
            round_id=answer.round_id,
            option_id=answer.option_id,
            client_answer_id=answer.client_answer_id,
            received_at=answer.received_at,
            is_replay=is_replay,
            client_elapsed_ms=answer.client_elapsed_ms,
            timing_basis=timing.basis,
        )

    @staticmethod
    def _answer_timing(policy: ScoringPolicy | None) -> AnswerTiming:
        if policy is None or not policy.is_approved:
            raise DomainInvariantError("approved_scoring_policy_not_found")
        try:
            return AnswerTiming.model_validate(
                {
                    "basis": policy.timing_basis,
                    "arrival_grace_ms": policy.arrival_grace_ms,
                    "clock_tolerance_ms": policy.clock_tolerance_ms,
                    "client_ahead_tolerance_ms": policy.client_ahead_tolerance_ms,
                }
            )
        except ValidationError as error:
            raise DomainInvariantError("answer_timing_policy_invalid") from error

    @classmethod
    def _round_answer_timing(cls, session: Session, round_: QuizRound) -> AnswerTiming:
        policy = session.scalar(
            select(ScoringPolicy)
            .join(Event, Event.scoring_policy_version == ScoringPolicy.version)
            .where(Event.id == round_.event_id)
        )
        return cls._answer_timing(policy)

    @staticmethod
    def _scoring_rule(session: Session, version: str) -> ScoringRule:
        policy = session.get(ScoringPolicy, version)
        if policy is None or not policy.is_approved:
            raise DomainInvariantError("approved_scoring_policy_not_found")
        timing = QuizDomainService._answer_timing(policy)
        return ScoringRule(
            version=policy.version,
            base_points=policy.base_points,
            initial_speed_bonus=policy.initial_speed_bonus,
            speed_bucket_ms=policy.speed_bucket_ms,
            speed_decrement_per_bucket=policy.speed_decrement_per_bucket,
            standard_duration_ms=policy.standard_duration_ms,
            timing_basis=timing.basis,
            arrival_grace_ms=timing.arrival_grace_ms,
            clock_tolerance_ms=timing.clock_tolerance_ms,
            client_ahead_tolerance_ms=timing.client_ahead_tolerance_ms,
        )

    @staticmethod
    def _elapsed_ms(
        round_: QuizRound,
        answer: Answer,
        timing_basis: AnswerTimingBasis = "server_received_v1",
    ) -> int:
        if timing_basis == "client_monotonic_v1":
            if answer.client_elapsed_ms is None:
                raise DomainInvariantError("client_elapsed_ms_missing")
            return answer.client_elapsed_ms
        if round_.open_at is None or answer.received_at is None:
            raise DomainInvariantError("answer_time_data_missing")
        return (answer.received_at - round_.open_at) // timedelta(milliseconds=1)

    @staticmethod
    def _store_option_stats(
        session: Session,
        round_id: UUID,
        options: list[QuestionOption],
        answer_rows: list[tuple[Answer, Participant]],
    ) -> None:
        total = len(answer_rows)
        for option in options:
            count = sum(1 for answer, _ in answer_rows if answer.option_id == option.id)
            percentage = Decimal("0")
            if total > 0:
                percentage = (Decimal(count) * Decimal(100) / Decimal(total)).quantize(
                    Decimal("0.01")
                )
            session.add(
                RoundOptionStat(
                    round_id=round_id,
                    option_id=option.id,
                    valid_response_count=count,
                    response_percentage=percentage,
                )
            )

    @staticmethod
    def _overall_standings(session: Session, event_id: UUID) -> list[OverallStanding]:
        rows = session.execute(
            select(ScoreLedger, Participant, QuizRound)
            .join(Participant, Participant.id == ScoreLedger.participant_id)
            .join(QuizRound, QuizRound.id == ScoreLedger.round_id)
            .where(
                Participant.event_id == event_id,
            )
        ).all()
        rounds = {round_.id: round_ for _, _, round_ in rows}
        practice_round_ids = {
            round_.id
            for round_ in rounds.values()
            if is_practice_round(quiz_round_number(session, round_))
        }
        accumulators: dict[UUID, StandingAccumulator] = {}
        for ledger, participant, _ in rows:
            accumulator = accumulators.setdefault(
                participant.id,
                StandingAccumulator(
                    participant_id=participant.id,
                    public_label=participant.public_label,
                ),
            )
            # 練習の回答履歴・単問得点は残し、総合得点と順位の同点判定だけから除く。
            if ledger.round_id in practice_round_ids:
                continue
            accumulator.total_points += ledger.total_points
            if ledger.is_correct:
                accumulator.correct_count += 1
                accumulator.correct_elapsed_ms += ledger.elapsed_ms or 0
        return rank_overall(
            [
                OverallStandingInput(
                    participant_id=value.participant_id,
                    public_label=value.public_label,
                    total_points=value.total_points,
                    correct_count=value.correct_count,
                    correct_elapsed_ms=value.correct_elapsed_ms,
                )
                for value in accumulators.values()
            ]
        )

    @staticmethod
    def _next_snapshot_version(session: Session, event_id: UUID) -> int:
        current = session.scalar(
            select(func.max(RankSnapshot.version)).where(RankSnapshot.event_id == event_id)
        )
        return int(current or 0) + 1
