"""HTTP audience処理と参加者WS認証を短命Sessionとasync DB I/Oへまとめる。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from sys import exception
from time import monotonic
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import Session
from sqlmodel.ext.asyncio.session import AsyncSession

from quiz_app.async_db import get_async_sessionmaker
from quiz_app.audience.auth_service import (
    ParticipantAuthService,
    ParticipantPrincipal,
    VerifiedParticipantAuthentication,
    get_participant_auth_service,
)
from quiz_app.audience.contracts import (
    AnswerStatusResponse,
    ParticipantStateResponse,
    PersonalResultResponse,
    SubmitAnswerRequest,
    TimeResponse,
)
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.audience.projection import AudienceProjectionService, get_audience_projection_service
from quiz_app.db import get_engine
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import AcceptedAnswer, SubmitAnswerCommand
from quiz_app.observability.answer_diagnostics import (
    bind_answer_diagnostic,
    diagnostic_reason,
    record_answer_event,
    record_answer_failure_stack,
)
from quiz_app.observability.request_timing import observe_stage, record_database_work
from quiz_app.realtime.state_cache import (
    COMMON_STATE_LOAD_TIMEOUT_SECONDS,
    CommonStateBuildExpiredError,
    CommonStateRefreshRequired,
)
from quiz_app.single_event import resolve_single_event_id

PARTICIPANT_STATE_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ParticipantRequestContext:
    """SessionやORM identity mapを後続transactionへ持ち越さない値context。"""

    event_id: UUID
    principal: ParticipantPrincipal


@dataclass(frozen=True, slots=True)
class CommittedParticipantAnswer:
    event_id: UUID
    answer: AcceptedAnswer


class AudienceAsyncService:
    """同期domain coreをrun_syncし、driverのDB待ちだけをasyncにする。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authentication: ParticipantAuthService,
        projection: AudienceProjectionService,
        domain: QuizDomainService,
    ) -> None:
        self._sessions = sessions
        self._authentication = authentication
        self._projection = projection
        self._domain = domain
        # 既存async poolの残りを回答・保存確認へ残し、接続取得前に復旧を制限する。
        self._recovery_slots = asyncio.Semaphore(2)
        self._clock_task: asyncio.Task[datetime] | None = None
        self._clock_started_at = 0.0

    @asynccontextmanager
    async def _recovery_deadline(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(COMMON_STATE_LOAD_TIMEOUT_SECONDS):
                yield
        except TimeoutError as error:
            raise AudienceServiceUnavailableError("participant_recovery_deferred") from error

    @asynccontextmanager
    async def _recovery_slot(self) -> AsyncIterator[None]:
        with observe_stage("recovery_wait"):
            await self._recovery_slots.acquire()
        try:
            yield
        finally:
            self._recovery_slots.release()

    def _participant_context(
        self,
        session: Session,
        session_token: str | None,
    ) -> ParticipantRequestContext:
        with observe_stage("participant_metadata"):
            event_id = resolve_single_event_id(session)
            if session_token is None:
                raise ParticipantSessionInvalidError("participant_session_missing")
            principal = self._authentication.authenticate_in_session(session, session_token)
            if principal.event_id != event_id:
                raise ParticipantSessionInvalidError("participant_session_event_mismatch")
            return ParticipantRequestContext(event_id=event_id, principal=principal)

    async def authenticate_realtime(
        self, session_token: str | None
    ) -> VerifiedParticipantAuthentication:
        """初回・周期WS認証を復旧GETと同じ枠へ入れ、同期運営threadを使わない。"""

        if session_token is None:
            raise ParticipantSessionInvalidError("participant_session_missing")
        async with self._recovery_deadline(), self._recovery_slot():
            record_database_work("sessions")
            async with self._sessions() as session:
                return await session.run_sync(self._authenticate_realtime, session_token)

    def _authenticate_realtime(
        self, session: Session, session_token: str
    ) -> VerifiedParticipantAuthentication:
        with observe_stage("participant_metadata"):
            event_id = resolve_single_event_id(session)
            verified = self._authentication.authenticate_realtime_in_session(session, session_token)
            if verified.principal.event_id != event_id:
                raise ParticipantSessionInvalidError("participant_session_event_mismatch")
            return verified

    async def get_participant_state(self, session_token: str | None) -> ParticipantStateResponse:
        # 未参加者の確認だけで復旧枠とDBを占有し、認証済みの復帰を遅らせない。
        if session_token is None:
            raise ParticipantSessionInvalidError("participant_session_missing")
        async with self._recovery_deadline():
            return await self._get_participant_state(session_token)

    async def _get_participant_state(self, session_token: str | None) -> ParticipantStateResponse:
        for attempt in range(PARTICIPANT_STATE_MAX_ATTEMPTS):
            try:
                async with self._recovery_slot():
                    record_database_work("sessions")
                    async with self._sessions() as session:
                        return await session.run_sync(self._participant_state, session_token)
            except CommonStateRefreshRequired as error:
                if attempt + 1 == PARTICIPANT_STATE_MAX_ATTEMPTS:
                    raise AudienceServiceUnavailableError("common_state_retry_exhausted") from error
                # 共有構築待ちはDB接続と復旧枠を返し、WS認証や他の復帰を塞がない。
                try:
                    with observe_stage("common_state_wait"):
                        await self._projection.prepare_common_state(error.event_id)
                except (CommonStateBuildExpiredError, TimeoutError) as preparation_error:
                    raise AudienceServiceUnavailableError(
                        "common_state_preparation_timed_out"
                    ) from preparation_error
        raise AudienceServiceUnavailableError("common_state_retry_exhausted")

    async def get_participant_result(self, session_token: str | None) -> PersonalResultResponse:
        async with self._recovery_deadline():
            for attempt in range(PARTICIPANT_STATE_MAX_ATTEMPTS):
                try:
                    async with self._recovery_slot():
                        record_database_work("sessions")
                        async with self._sessions() as session:
                            return await session.run_sync(self._participant_result, session_token)
                except CommonStateRefreshRequired as error:
                    if attempt + 1 == PARTICIPANT_STATE_MAX_ATTEMPTS:
                        raise AudienceServiceUnavailableError(
                            "common_state_retry_exhausted"
                        ) from error
                    try:
                        with observe_stage("common_state_wait"):
                            await self._projection.prepare_common_state(error.event_id)
                    except (CommonStateBuildExpiredError, TimeoutError) as preparation_error:
                        raise AudienceServiceUnavailableError(
                            "common_state_preparation_timed_out"
                        ) from preparation_error
        raise AudienceServiceUnavailableError("common_state_retry_exhausted")

    def _participant_result(
        self, session: Session, session_token: str | None
    ) -> PersonalResultResponse:
        context = self._participant_context(session, session_token)
        return self._projection.get_participant_result_in_session(session, context.principal)

    def _participant_state(
        self,
        session: Session,
        session_token: str | None,
    ) -> ParticipantStateResponse:
        context = self._participant_context(session, session_token)
        with observe_stage("state_projection"):
            return self._projection.get_participant_state_in_session(session, context.principal)

    async def get_answer_status(
        self,
        session_token: str | None,
        client_answer_id: UUID,
    ) -> AnswerStatusResponse:
        record_database_work("sessions")
        async with self._sessions() as session:
            return await session.run_sync(self._answer_status, session_token, client_answer_id)

    def _answer_status(
        self,
        session: Session,
        session_token: str | None,
        client_answer_id: UUID,
    ) -> AnswerStatusResponse:
        context = self._participant_context(session, session_token)
        bind_answer_diagnostic(context.principal.session_id, client_answer_id)
        with observe_stage("status_projection"):
            return self._projection.get_answer_status_in_session(
                session, context.principal, client_answer_id
            )

    async def get_server_time(self) -> TimeResponse:
        joined_at = monotonic()
        task = self._clock_task
        if task is None:
            self._clock_started_at = joined_at
            task = asyncio.create_task(self._load_server_time(), name="shared-database-clock")
            self._clock_task = task

            def finished(completed: asyncio.Task[datetime]) -> None:
                if self._clock_task is completed:
                    self._clock_task = None
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        sample_age_ms = max(0.0, joined_at - self._clock_started_at) * 1000.0
        # 完了済み時刻は保持せず、同時進行の照会だけ共有する。
        try:
            async with asyncio.timeout(COMMON_STATE_LOAD_TIMEOUT_SECONDS):
                server_time = await asyncio.shield(task)
                # 遅い合流が古いDB標本を短RTTと誤認しないよう、待機済み時間を返す。
                return TimeResponse(server_time=server_time, shared_sample_age_ms=sample_age_ms)
        except TimeoutError as error:
            raise AudienceServiceUnavailableError("database_clock_deferred") from error

    async def _load_server_time(self) -> datetime:
        async with asyncio.timeout(COMMON_STATE_LOAD_TIMEOUT_SECONDS):
            record_database_work("sessions")
            async with self._sessions() as session:
                return await session.run_sync(self._server_time)

    async def close(self) -> None:
        task = self._clock_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _server_time(self, session: Session) -> datetime:
        # DB時刻を唯一開催の検証後に取得し、CPU clockの代用にしない。
        resolve_single_event_id(session)
        return self._projection.get_server_time_in_session(session)

    async def submit_answer(
        self,
        session_token: str | None,
        round_id: UUID,
        request: SubmitAnswerRequest,
    ) -> CommittedParticipantAnswer:
        with observe_stage("answer_transaction"):
            return await self._commit_answer(session_token, round_id, request)

    async def _commit_answer(
        self,
        session_token: str | None,
        round_id: UUID,
        request: SubmitAnswerRequest,
    ) -> CommittedParticipantAnswer:
        context: ParticipantRequestContext | None = None
        command: SubmitAnswerCommand | None = None
        try:
            record_database_work("sessions")
            async with self._sessions() as session:
                phase: Literal["body", "commit"] = "body"
                entry_failure = exception()
                try:
                    async with session.begin():
                        # 認証SELECTのautobeginより先に保存transactionを開始する。
                        context = await session.run_sync(self._participant_context, session_token)
                        bind_answer_diagnostic(
                            context.principal.session_id, request.client_answer_id
                        )
                        command = SubmitAnswerCommand(
                            participant_id=context.principal.participant_id,
                            round_id=round_id,
                            round_version=request.round_version,
                            option_id=request.option_id,
                            client_answer_id=request.client_answer_id,
                            client_elapsed_ms=request.client_elapsed_ms,
                        )
                        record_answer_event("transaction_started")
                        accepted = await session.run_sync(
                            self._domain.submit_answer_in_transaction,
                            command,
                            expected_event_id=context.event_id,
                        )
                        phase = "commit"
                        record_answer_event("commit_started", is_replay=accepted.is_replay)
                    # begin退出のACKをSession close前に採り、DB commit瞬間とは区別する。
                    record_answer_event("commit_acknowledged", is_replay=accepted.is_replay)
                finally:
                    failure = exception()
                    if failure is not None and failure is not entry_failure:
                        # rollback完了やDB commit瞬間は、この退出観測から断定しない。
                        record_answer_event(
                            "transaction_failed",
                            reason=diagnostic_reason(failure),
                            transaction_phase=phase,
                        )
                        record_answer_failure_stack(failure, "transaction_failed")
            return CommittedParticipantAnswer(event_id=context.event_id, answer=accepted)
        except IntegrityError:
            if context is None or command is None:
                raise
            record_database_work("sessions")
            async with self._sessions() as retry_session:
                record_answer_event("integrity_recheck_started")
                existing = await retry_session.run_sync(
                    self._domain.get_answer_replay_in_session, command
                )
                record_answer_event(
                    "integrity_recheck_finished",
                    status="NotStored" if existing is None else "AcceptedLocked",
                    is_replay=existing.is_replay if existing is not None else None,
                )
                if existing is None:
                    raise
                return CommittedParticipantAnswer(event_id=context.event_id, answer=existing)


@lru_cache
def _cached_audience_async_service() -> AudienceAsyncService:
    return AudienceAsyncService(
        get_async_sessionmaker(),
        get_participant_auth_service(),
        get_audience_projection_service(),
        QuizDomainService(get_engine()),
    )


async def get_audience_async_service() -> AudienceAsyncService:
    """軽いfactory取得自体を同期workerの待ち列へ追加しない。"""

    return _cached_audience_async_service()


def reset_audience_async_service() -> None:
    """停止済みEngineを次のlifespanへ持ち越さない。"""

    _cached_audience_async_service.cache_clear()


async def close_audience_async_service() -> None:
    """共有中の時計照会をEngine disposeより先に回収する。"""

    if _cached_audience_async_service.cache_info().currsize:
        await _cached_audience_async_service().close()
