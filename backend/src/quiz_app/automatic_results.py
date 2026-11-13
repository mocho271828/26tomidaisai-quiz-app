import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from traceback import walk_tb
from typing import Literal, Protocol
from uuid import UUID

import logfire
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, Select, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session

from quiz_app.domain.errors import DomainError, DomainInvariantError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import Event, OperationAudit, QuizRound, ScoringPolicy
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.quiz_configuration import get_quiz_settings
from quiz_app.realtime.contracts import InvalidationCause, RealtimeAudience

AUTOMATIC_AGGREGATE_OPERATION = "aggregate_round_results"
AUTOMATIC_AGGREGATE_REASON = "automatic_after_answer_deadline"
AUTOMATIC_AGGREGATE_DOMAIN_FAILURE = "automatic_aggregate_domain_failure"
AUTOMATIC_AGGREGATE_INTEGRITY_FAILURE = "automatic_aggregate_integrity_failure"
AUTOMATIC_AGGREGATE_IDLE_SECONDS = 0.25
AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS = (0.1, 0.5, 1.0, 2.0)
AUTOMATIC_DOMAIN_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,79}")
AUTOMATIC_AGGREGATE_SOURCE_STATES = (
    RoundState.SCHEDULED,
    RoundState.OPEN,
    RoundState.AWAITING_VALIDATION,
)

AutomaticAggregateStatus = Literal["aggregated", "void_candidate"]
SessionFactory = Callable[[], Session]


class ResultInvalidationPublisher(Protocol):
    def notify(
        self,
        event_id: UUID,
        audiences: Sequence[RealtimeAudience],
        *,
        cause: InvalidationCause,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AutomaticAggregateOutcome:
    status: AutomaticAggregateStatus
    event_id: UUID
    round_id: UUID
    round_version: int


class ResultFailureFrame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str
    line: int
    function: str


def result_failure_stack(error: SQLAlchemyError | DomainError) -> list[ResultFailureFrame]:
    """例外本文・cause・locals・source行を含めず、結果処理の失敗位置だけを返す。"""

    stack: list[ResultFailureFrame] = []
    for frame, line_number in walk_tb(error.__traceback__):
        module = frame.f_globals.get("__name__")
        stack.append(
            ResultFailureFrame(
                module=module if isinstance(module, str) else "unknown",
                line=line_number,
                function=frame.f_code.co_name,
            )
        )
    return stack


def automatic_aggregate_candidate_statement() -> Select[tuple[QuizRound]]:
    """締切到達順に1件を取り、他processには同じRoundを処理させない。"""

    settings = get_quiz_settings()
    return (
        select(QuizRound)
        .join(Event, Event.id == QuizRound.event_id)
        .join(ScoringPolicy, ScoringPolicy.version == Event.scoring_policy_version)
        .where(
            Event.state == EventState.IN_PROGRESS,
            ScoringPolicy.version == settings.scoring.version,
            ScoringPolicy.timing_basis == settings.timing.basis,
            QuizRound.result_publication == RoundResultPublication.WITHHELD,
            QuizRound.standard_close_at.is_not(None),
            QuizRound.standard_close_at + ScoringPolicy.arrival_grace_ms * timedelta(milliseconds=1)
            <= func.clock_timestamp(),
            QuizRound.state.in_(AUTOMATIC_AGGREGATE_SOURCE_STATES),
        )
        .order_by(QuizRound.standard_close_at, QuizRound.id)
        .limit(1)
        .with_for_update(of=QuizRound, skip_locked=True)
    )


class AutomaticResultAggregator:
    """回答の到着猶予後に未公開Roundを排他的に集計する。"""

    def __init__(
        self,
        engine: Engine,
        domain_service: QuizDomainService,
        realtime_publisher: ResultInvalidationPublisher,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._engine = engine
        self._domain_service = domain_service
        self._realtime_publisher = realtime_publisher
        self._session_factory = session_factory or (
            lambda: Session(self._engine, expire_on_commit=False)
        )

    def process_next(self) -> AutomaticAggregateOutcome | None:
        """対象Roundを最大1件処理し、commit後にprojectionを無効化する。"""

        outcome: AutomaticAggregateOutcome | None = None
        with self._session_factory() as session, session.begin():
            round_ = session.scalar(automatic_aggregate_candidate_statement())
            if round_ is None:
                return None

            expected_version = round_.version
            try:
                # 集計途中のinsertをsavepointへ閉じ、失敗時は要確認状態だけを残す。
                with session.begin_nested():
                    result = self._domain_service.aggregate_round_results_in_transaction(
                        session,
                        round_.id,
                        expected_version,
                    )
            except (DomainInvariantError, IntegrityError) as error:
                failure_reason = self._failure_reason(error)
                logfire.error(
                    "automatic round result aggregation failed permanently",
                    error_type=type(error).__name__,
                    stack=result_failure_stack(error),
                )
                failed = self._domain_service.mark_round_results_void_candidate_in_transaction(
                    session,
                    round_.id,
                    expected_version,
                    failure_reason,
                )
                session.add(
                    self._audit(
                        event_id=failed.event_id,
                        round_id=failed.round_id,
                        before_state=RoundState.AWAITING_VALIDATION.value,
                        after_state=failed.round_state.value,
                        outcome="failed",
                        reason=f"{AUTOMATIC_AGGREGATE_REASON}:{failure_reason}",
                        occurred_at=self._database_time(session),
                    )
                )
                outcome = AutomaticAggregateOutcome(
                    status="void_candidate",
                    event_id=failed.event_id,
                    round_id=failed.round_id,
                    round_version=failed.round_version,
                )
            else:
                session.add(
                    self._audit(
                        event_id=result.event_id,
                        round_id=result.round_id,
                        before_state=RoundState.AWAITING_VALIDATION.value,
                        after_state=RoundState.READY_TO_REVEAL.value,
                        outcome="succeeded",
                        reason=AUTOMATIC_AGGREGATE_REASON,
                        occurred_at=self._database_time(session),
                    )
                )
                outcome = AutomaticAggregateOutcome(
                    status="aggregated",
                    event_id=result.event_id,
                    round_id=result.round_id,
                    round_version=result.round_version,
                )

        if outcome is None:
            raise DomainInvariantError("automatic_aggregate_outcome_missing")
        self._realtime_publisher.notify(
            outcome.event_id,
            ("operator",),
            cause="automatic_result",
        )
        return outcome

    async def run(self, stop_requested: asyncio.Event) -> None:
        """DB処理をthreadへ逃がし、DB障害時は上限付きbackoffで再試行する。"""

        retry_index = 0
        while not stop_requested.is_set():
            try:
                outcome = await asyncio.to_thread(self.process_next)
            except SQLAlchemyError as error:
                retry_delay = AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[
                    min(retry_index, len(AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS) - 1)
                ]
                retry_index += 1
                logfire.error(
                    "automatic result aggregator database operation failed",
                    error_type=type(error).__name__,
                    stack=result_failure_stack(error),
                    retry_delay_seconds=retry_delay,
                )
                await self._wait(stop_requested, retry_delay)
                continue
            except DomainError as error:
                logfire.error(
                    "automatic result aggregator domain operation escaped",
                    error_type=type(error).__name__,
                    stack=result_failure_stack(error),
                )
                await self._wait(stop_requested, AUTOMATIC_AGGREGATE_RETRY_DELAYS_SECONDS[-1])
                continue

            retry_index = 0
            if outcome is None:
                await self._wait(stop_requested, AUTOMATIC_AGGREGATE_IDLE_SECONDS)

    @staticmethod
    async def _wait(stop_requested: asyncio.Event, delay_seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=delay_seconds)
        except TimeoutError:
            return

    @staticmethod
    def _failure_reason(error: DomainError | IntegrityError) -> str:
        if isinstance(error, DomainError):
            domain_code = str(error).strip()
            if AUTOMATIC_DOMAIN_ERROR_CODE_PATTERN.fullmatch(domain_code) is not None:
                return f"{AUTOMATIC_AGGREGATE_DOMAIN_FAILURE}:{domain_code}"
            return AUTOMATIC_AGGREGATE_DOMAIN_FAILURE
        return AUTOMATIC_AGGREGATE_INTEGRITY_FAILURE

    @staticmethod
    def _database_time(session: Session) -> datetime:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise DomainInvariantError("database_clock_unavailable")
        return database_time

    @staticmethod
    def _audit(
        *,
        event_id: UUID,
        round_id: UUID,
        before_state: str,
        after_state: str,
        outcome: Literal["succeeded", "failed"],
        reason: str,
        occurred_at: datetime,
    ) -> OperationAudit:
        return OperationAudit(
            event_id=event_id,
            round_id=round_id,
            account_id=None,
            operation=AUTOMATIC_AGGREGATE_OPERATION,
            target_type="quiz_round",
            target_id=str(round_id),
            before_state=before_state,
            after_state=after_state,
            reason=reason,
            outcome=outcome,
            idempotency_key=None,
            occurred_at=occurred_at,
        )
