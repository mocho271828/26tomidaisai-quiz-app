import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Literal
from uuid import UUID

import logfire
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Engine, func, select, text
from sqlmodel import Session

from quiz_app.config import get_settings
from quiz_app.db import get_engine
from quiz_app.domain.award_ceremony import set_award_ceremony_in_transaction
from quiz_app.domain.errors import (
    DomainInvariantError,
    InvalidStateTransitionError,
    ResourceNotFoundError,
    RoundBackupFailedError,
)
from quiz_app.domain.final_round import set_final_round_in_transaction
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import Event, IdempotencyRecord, OperationAudit, QuizRound
from quiz_app.models.enums import RoundState
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import (
    AwardCeremonyMutationRequest,
    NextRoundMutationRequest,
    OperatorMutationRequest,
    OperatorMutationResponse,
    QuestionSelectionMutationRequest,
    ResultVisibilityMutationRequest,
    ScheduleRoundMutationRequest,
)
from quiz_app.operator.errors import IdempotencyKeyConflictError
from quiz_app.operator.final_round_contracts import FinalRoundMutationRequest
from quiz_app.quiz_configuration import (
    ResultVisibility,
    VisibilityMutationTarget,
    require_event_configuration,
    visibility_state,
)
from quiz_app.round_backup import RoundBackupError, RoundBackupReceipt, RoundBackupService

IDEMPOTENCY_RETENTION_HOURS = 24
ROUND_BACKUP_FAILURE_STATUS = 503
MutationOperation = Literal[
    "begin_preparation",
    "select_round_question",
    "schedule_round",
    "retry_round_results",
    "publish_round_results",
    "advance_round",
    "complete_event",
    "set_result_visibility",
    "set_final_round",
    "set_award_ceremony",
]


class _StoredMutationFailure(BaseModel):
    """状態変更を伴う失敗を安全に冪等再生するための内部sentinel。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["round_backup_failed"]
    event_id: UUID


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    response: OperatorMutationResponse
    event_id: UUID
    round_id: UUID | None
    target_type: str
    target_id: str
    before_state: str | None
    after_state: str | None


MutationHandler = Callable[[Session], MutationOutcome]


class OperatorMutationService:
    """運営mutationへ冪等性・監査・domain更新のtransaction境界を付与する。"""

    def __init__(
        self,
        engine: Engine,
        domain_service: QuizDomainService,
        round_backup_service: RoundBackupService | None = None,
    ) -> None:
        self._engine = engine
        self._domain_service = domain_service
        self._round_backup_service = round_backup_service

    def begin_preparation(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            round_ = self._round_for_update(session, round_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            before_state = round_.state.value
            result = self._domain_service.begin_preparation_in_transaction(
                session,
                round_id,
                request.expected_version,
            )
            event = self._required_event(session, round_.event_id)
            response = OperatorMutationResponse(
                operation="begin_preparation",
                event_id=event.id,
                event_version=event.version,
                round_id=result.round_id,
                round_version=result.round_version,
                round_state=result.round_state,
                result_visibility=visibility_state(
                    require_event_configuration(session, event).record
                ),
            )
            return MutationOutcome(
                response=response,
                event_id=event.id,
                round_id=round_.id,
                target_type="quiz_round",
                target_id=str(round_.id),
                before_state=before_state,
                after_state=result.round_state.value,
            )

        return self._execute(
            principal,
            operation="begin_preparation",
            target_id=round_id,
            request=request,
            handler=mutate,
        )

    def select_round_question(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: QuestionSelectionMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            round_ = self._round_for_update(session, round_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            previous_question_id = round_.question_id
            result = self._domain_service.select_round_question_in_transaction(
                session,
                round_id,
                request.expected_version,
                request.question_id,
            )
            event = self._required_event(session, result.event_id)
            response = OperatorMutationResponse(
                operation="select_round_question",
                event_id=result.event_id,
                event_version=result.event_version,
                round_id=result.round_id,
                round_version=result.round_version,
                round_state=result.round_state,
                result_visibility=visibility_state(
                    require_event_configuration(session, event).record
                ),
            )
            return MutationOutcome(
                response=response,
                event_id=event.id,
                round_id=round_.id,
                target_type="quiz_round_question",
                target_id=str(round_.id),
                before_state=str(previous_question_id),
                after_state=str(result.question_id),
            )

        return self._execute(
            principal,
            operation="select_round_question",
            target_id=round_id,
            request=request,
            handler=mutate,
        )

    def schedule_round(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: ScheduleRoundMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            round_ = self._round_for_update(session, round_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            before_state = round_.state.value
            result = self._domain_service.schedule_round_in_transaction(
                session,
                round_id,
                request.expected_version,
                target_open_at=request.target_open_at,
            )
            event = self._required_event(session, round_.event_id)
            response = OperatorMutationResponse(
                operation="schedule_round",
                event_id=event.id,
                event_version=event.version,
                round_id=result.round_id,
                round_version=result.version,
                round_state=round_.state,
                result_visibility=visibility_state(
                    require_event_configuration(session, event).record
                ),
                open_at=result.open_at,
                standard_close_at=result.standard_close_at,
            )
            return MutationOutcome(
                response=response,
                event_id=event.id,
                round_id=round_.id,
                target_type="quiz_round",
                target_id=str(round_.id),
                before_state=before_state,
                after_state=round_.state.value,
            )

        return self._execute(
            principal,
            operation="schedule_round",
            target_id=round_id,
            request=request,
            handler=mutate,
        )

    def retry_round_results(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            round_ = self._round_for_update(session, round_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            before_state = round_.state.value
            result = self._domain_service.retry_round_results_in_transaction(
                session,
                round_id,
                request.expected_version,
            )
            event = self._required_event(session, result.event_id)
            response = OperatorMutationResponse(
                operation="retry_round_results",
                event_id=result.event_id,
                event_version=result.event_version,
                round_id=result.round_id,
                round_version=result.round_version,
                round_state=result.round_state,
                result_visibility=visibility_state(
                    require_event_configuration(session, event).record
                ),
            )
            return MutationOutcome(
                response=response,
                event_id=event.id,
                round_id=round_.id,
                target_type="quiz_round",
                target_id=str(round_.id),
                before_state=before_state,
                after_state=round_.state.value,
            )

        return self._execute(
            principal,
            operation="retry_round_results",
            target_id=round_id,
            request=request,
            handler=mutate,
        )

    def publish_round_results(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            round_ = self._round_for_update(session, round_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            before_state = round_.state.value
            result = self._domain_service.publish_round_results_in_transaction(
                session, round_id, request.expected_version
            )
            event = self._required_event(session, result.event_id)
            response = OperatorMutationResponse(
                operation="publish_round_results",
                event_id=result.event_id,
                event_version=result.event_version,
                round_id=result.round_id,
                round_version=result.round_version,
                round_state=round_.state,
                result_visibility=visibility_state(
                    require_event_configuration(session, event).record
                ),
                valid_answer_count=result.valid_answer_count,
                ranking_row_count=result.ranking_row_count,
            )
            return MutationOutcome(
                response=response,
                event_id=event.id,
                round_id=round_.id,
                target_type="quiz_round",
                target_id=str(round_.id),
                before_state=before_state,
                after_state=round_.state.value,
            )

        return self._execute(
            principal,
            operation="publish_round_results",
            target_id=round_id,
            request=request,
            handler=mutate,
        )

    def set_result_visibility(
        self,
        principal: OperatorPrincipal,
        event_id: UUID,
        request: ResultVisibilityMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            event = self._event_for_update(session, event_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            previous_visibility = visibility_state(
                require_event_configuration(session, event).record
            )
            before_state = visibility_audit_state(previous_visibility, request.target)
            result = self._domain_service.set_result_visibility_in_transaction(
                session,
                event_id,
                request.expected_version,
                request.target,
                request.visible,
            )
            response = OperatorMutationResponse(
                operation="set_result_visibility",
                event_id=result.event_id,
                event_version=result.event_version,
                result_visibility=result.visibility,
            )
            return MutationOutcome(
                response=response,
                event_id=event.id,
                round_id=None,
                target_type="result_visibility",
                target_id=f"{event.id}:{request.target}",
                before_state=before_state,
                after_state=visibility_audit_state(result.visibility, request.target),
            )

        return self._execute(
            principal,
            operation="set_result_visibility",
            target_id=event_id,
            request=request,
            handler=mutate,
        )

    def set_final_round(
        self,
        principal: OperatorPrincipal,
        event_id: UUID,
        request: FinalRoundMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            result = set_final_round_in_transaction(
                session,
                event_id=event_id,
                round_id=request.round_id,
                expected_version=request.expected_version,
                is_final=request.is_final,
            )
            return MutationOutcome(
                response=OperatorMutationResponse(
                    operation="set_final_round",
                    event_id=result.event_id,
                    event_version=result.event_version,
                    result_visibility=result.result_visibility,
                ),
                event_id=result.event_id,
                # Event取得後の監査INSERTでRoundのFK lockを取ると、
                # 既存進行処理のRound→Eventと逆順になる。Round IDは前後状態に残す。
                round_id=None,
                target_type="final_round",
                target_id=str(event_id),
                before_state=(
                    str(result.previous_round_id) if result.previous_round_id is not None else None
                ),
                after_state=str(result.final_round_id)
                if result.final_round_id is not None
                else None,
            )

        return self._execute(
            principal,
            operation="set_final_round",
            target_id=event_id,
            request=request,
            handler=mutate,
        )

    def set_award_ceremony(
        self,
        principal: OperatorPrincipal,
        event_id: UUID,
        request: AwardCeremonyMutationRequest,
    ) -> OperatorMutationResponse:
        def mutate(session: Session) -> MutationOutcome:
            result = set_award_ceremony_in_transaction(
                session,
                event_id=event_id,
                expected_version=request.expected_version,
                phase=request.phase,
            )
            return MutationOutcome(
                response=OperatorMutationResponse(
                    operation="set_award_ceremony",
                    event_id=result.event_id,
                    event_version=result.event_version,
                    result_visibility=result.result_visibility,
                ),
                event_id=result.event_id,
                round_id=None,
                target_type="award_ceremony",
                target_id=str(event_id),
                before_state=result.previous_phase,
                after_state=result.phase,
            )

        return self._execute(
            principal,
            operation="set_award_ceremony",
            target_id=event_id,
            request=request,
            handler=mutate,
        )

    def advance_round(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: NextRoundMutationRequest,
    ) -> OperatorMutationResponse:
        """暗号化backupを検証し、指定した未使用問題で新Roundを作成する。"""

        return self._execute_round_progression(
            principal,
            operation="advance_round",
            round_id=round_id,
            request=request,
            next_question_id=request.question_id,
        )

    def complete_event(
        self,
        principal: OperatorPrincipal,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        """最終Roundの暗号化backupを検証し、Eventを完了する。"""

        return self._execute_round_progression(
            principal,
            operation="complete_event",
            round_id=round_id,
            request=request,
            next_question_id=None,
        )

    def _execute_round_progression(
        self,
        principal: OperatorPrincipal,
        *,
        operation: Literal["advance_round", "complete_event"],
        round_id: UUID,
        request: OperatorMutationRequest,
        next_question_id: UUID | None,
    ) -> OperatorMutationResponse:
        """backup I/OとRound/Event更新を同じ排他transactionで確定する。"""

        request_hash = mutation_request_hash(operation, round_id, request)
        backup_error: RoundBackupError | None = None
        failed_event_id: UUID | None = None
        response: OperatorMutationResponse | None = None
        with Session(self._engine) as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": advisory_lock_key(principal.account_id, request.idempotency_key)},
            )
            existing = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.account_id == principal.account_id,
                    IdempotencyRecord.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                return self._replay(existing, operation, request_hash)

            self._require_within_execution_deadline(session, request.execute_not_after)

            round_ = self._round_for_update(session, round_id)
            self._require_within_execution_deadline(session, request.execute_not_after)
            before_state = round_.state.value
            before_event_state = self._required_event(session, round_.event_id).state.value
            if next_question_id is not None:
                self._domain_service.validate_next_round_question_in_transaction(
                    session,
                    round_id,
                    next_question_id,
                )
            started = self._domain_service.begin_round_backup_in_transaction(
                session,
                round_id,
                request.expected_version,
            )
            pending_round = self._round_for_update(session, round_id)
            if self._round_backup_service is None:
                raise DomainInvariantError("round_backup_service_not_configured")
            try:
                receipt = self._round_backup_service.create_and_verify(
                    session,
                    pending_round,
                    artifact_suffix=operation,
                    idempotency_key=request.idempotency_key,
                )
            except RoundBackupError as error:
                logfire.exception(
                    "round backup creation or verification failed",
                    operation=operation,
                    event_id=str(started.event_id),
                    round_id=str(round_id),
                )
                self._require_within_execution_deadline(session, request.execute_not_after)
                failed = self._domain_service.fail_round_backup_in_transaction(session, round_id)
                self._require_within_execution_deadline(session, request.execute_not_after)
                event = self._required_event(session, failed.event_id)
                database_time = self._database_time(session)
                session.add(
                    OperationAudit(
                        event_id=event.id,
                        round_id=round_id,
                        account_id=principal.account_id,
                        operation=operation,
                        target_type="quiz_round",
                        target_id=str(round_id),
                        before_state=before_state,
                        after_state=failed.round_state.value,
                        reason="",
                        outcome="failed",
                        idempotency_key=request.idempotency_key,
                        occurred_at=database_time,
                    )
                )
                session.add(
                    IdempotencyRecord(
                        account_id=principal.account_id,
                        idempotency_key=request.idempotency_key,
                        operation=operation,
                        request_hash=request_hash,
                        response_status=ROUND_BACKUP_FAILURE_STATUS,
                        response_json=_StoredMutationFailure(
                            code="round_backup_failed",
                            event_id=event.id,
                        ).model_dump_json(),
                        resulting_version=failed.round_version,
                        created_at=database_time,
                        expires_at=database_time + timedelta(hours=IDEMPOTENCY_RETENTION_HOURS),
                    )
                )
                backup_error = error
                failed_event_id = event.id
            else:
                self._require_within_execution_deadline(session, request.execute_not_after)
                response, after_state = self._complete_round_progression(
                    session,
                    operation=operation,
                    round_id=round_id,
                    next_question_id=next_question_id,
                )
                self._require_within_execution_deadline(session, request.execute_not_after)
                database_time = self._database_time(session)
                self._record_backup_audit(
                    session,
                    principal=principal,
                    receipt=receipt,
                    event_id=response.event_id,
                    round_id=round_id,
                    occurred_at=database_time,
                )
                session.add(
                    OperationAudit(
                        event_id=response.event_id,
                        round_id=round_id,
                        account_id=principal.account_id,
                        operation=operation,
                        target_type="event" if operation == "complete_event" else "quiz_round",
                        target_id=(
                            str(response.event_id)
                            if operation == "complete_event"
                            else str(round_id)
                        ),
                        before_state=(
                            before_event_state if operation == "complete_event" else before_state
                        ),
                        after_state=after_state,
                        reason="",
                        outcome="succeeded",
                        idempotency_key=request.idempotency_key,
                        occurred_at=database_time,
                    )
                )
                session.add(
                    IdempotencyRecord(
                        account_id=principal.account_id,
                        idempotency_key=request.idempotency_key,
                        operation=operation,
                        request_hash=request_hash,
                        response_status=200,
                        response_json=response.model_dump_json(),
                        resulting_version=mutation_resulting_version(operation, response),
                        created_at=database_time,
                        expires_at=database_time + timedelta(hours=IDEMPOTENCY_RETENTION_HOURS),
                    )
                )

        if backup_error is not None:
            if failed_event_id is None:
                raise DomainInvariantError("round_backup_failed_event_missing")
            raise RoundBackupFailedError(
                "round_backup_verification_failed",
                event_id=failed_event_id,
            ) from backup_error
        if response is None:
            raise DomainInvariantError("round_progression_response_missing")
        logfire.info(
            "operator round progression completed",
            operation=operation,
            event_id=str(response.event_id),
            round_id=str(response.round_id) if response.round_id is not None else None,
        )
        return response

    def _complete_round_progression(
        self,
        session: Session,
        *,
        operation: Literal["advance_round", "complete_event"],
        round_id: UUID,
        next_question_id: UUID | None,
    ) -> tuple[OperatorMutationResponse, str]:
        if operation == "advance_round":
            if next_question_id is None:
                raise DomainInvariantError("next_question_id_missing")
            result = self._domain_service.advance_round_after_backup_in_transaction(
                session,
                round_id,
                next_question_id,
            )
            event = self._required_event(session, result.event_id)
            return (
                OperatorMutationResponse(
                    operation="advance_round",
                    event_id=event.id,
                    event_version=result.event_version,
                    round_id=result.new_round_id,
                    round_version=result.new_round_version,
                    round_state=result.new_round_state,
                    result_visibility=visibility_state(
                        require_event_configuration(session, event).record
                    ),
                ),
                RoundState.ROUND_BACKUP_VALIDATED.value,
            )

        result = self._domain_service.complete_event_after_backup_in_transaction(
            session,
            round_id,
        )
        event = self._required_event(session, result.event_id)
        return (
            OperatorMutationResponse(
                operation="complete_event",
                event_id=event.id,
                event_version=result.event_version,
                round_id=result.round_id,
                round_version=result.round_version,
                round_state=result.round_state,
                result_visibility=visibility_state(
                    require_event_configuration(session, event).record
                ),
            ),
            event.state.value,
        )

    @staticmethod
    def _record_backup_audit(
        session: Session,
        *,
        principal: OperatorPrincipal,
        receipt: RoundBackupReceipt,
        event_id: UUID,
        round_id: UUID,
        occurred_at: datetime,
    ) -> None:
        evidence = json.dumps(
            {
                "artifact_name": receipt.artifact_name,
                "counts": receipt.counts.model_dump(mode="json"),
                "plaintext_sha256": receipt.plaintext_sha256,
                "round_version": receipt.round_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        session.add(
            OperationAudit(
                event_id=event_id,
                round_id=round_id,
                account_id=principal.account_id,
                operation="validate_round_backup",
                target_type="round_backup_artifact",
                target_id=receipt.plaintext_sha256,
                before_state=RoundState.ROUND_BACKUP_PENDING.value,
                after_state=RoundState.ROUND_BACKUP_VALIDATED.value,
                reason=evidence,
                outcome="succeeded",
                idempotency_key=None,
                occurred_at=occurred_at,
            )
        )

    def _execute(
        self,
        principal: OperatorPrincipal,
        *,
        operation: MutationOperation,
        target_id: UUID,
        request: OperatorMutationRequest,
        handler: MutationHandler,
    ) -> OperatorMutationResponse:
        request_hash = mutation_request_hash(operation, target_id, request)
        with Session(self._engine) as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": advisory_lock_key(principal.account_id, request.idempotency_key)},
            )
            existing = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.account_id == principal.account_id,
                    IdempotencyRecord.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                return self._replay(existing, operation, request_hash)

            self._require_within_execution_deadline(session, request.execute_not_after)

            outcome = handler(session)
            self._require_within_execution_deadline(session, request.execute_not_after)
            database_time = self._database_time(session)
            session.add(
                OperationAudit(
                    event_id=outcome.event_id,
                    round_id=outcome.round_id,
                    account_id=principal.account_id,
                    operation=operation,
                    target_type=outcome.target_type,
                    target_id=outcome.target_id,
                    before_state=outcome.before_state,
                    after_state=outcome.after_state,
                    reason="",
                    outcome="succeeded",
                    idempotency_key=request.idempotency_key,
                    occurred_at=database_time,
                )
            )
            session.add(
                IdempotencyRecord(
                    account_id=principal.account_id,
                    idempotency_key=request.idempotency_key,
                    operation=operation,
                    request_hash=request_hash,
                    response_status=200,
                    response_json=outcome.response.model_dump_json(),
                    resulting_version=mutation_resulting_version(
                        operation,
                        outcome.response,
                    ),
                    created_at=database_time,
                    expires_at=database_time + timedelta(hours=IDEMPOTENCY_RETENTION_HOURS),
                )
            )
            response = outcome.response

        logfire.info(
            "operator mutation completed",
            operation=operation,
            event_id=str(response.event_id),
            round_id=str(response.round_id) if response.round_id is not None else None,
        )
        return response

    def _require_within_execution_deadline(
        self,
        session: Session,
        execute_not_after: datetime | None,
    ) -> None:
        if execute_not_after is None:
            return
        if self._database_time(session) > execute_not_after:
            raise InvalidStateTransitionError("mutation_deadline_elapsed")

    @staticmethod
    def _replay(
        record: IdempotencyRecord,
        operation: MutationOperation,
        request_hash: str,
    ) -> OperatorMutationResponse:
        if record.operation != operation or not hmac.compare_digest(
            record.request_hash,
            request_hash,
        ):
            raise IdempotencyKeyConflictError("idempotency_key_payload_conflict")
        if record.response_status == ROUND_BACKUP_FAILURE_STATUS:
            try:
                failure = _StoredMutationFailure.model_validate_json(record.response_json)
            except ValidationError as error:
                raise DomainInvariantError("stored_idempotency_failure_invalid") from error
            if failure.code == "round_backup_failed":
                raise RoundBackupFailedError(
                    "round_backup_verification_failed",
                    event_id=failure.event_id,
                )
        if record.response_status != 200:
            raise DomainInvariantError("stored_idempotency_status_invalid")
        try:
            response = OperatorMutationResponse.model_validate_json(record.response_json)
        except ValidationError as error:
            raise DomainInvariantError("stored_idempotency_response_invalid") from error
        return response.model_copy(update={"is_replay": True})

    @staticmethod
    def _database_time(session: Session) -> datetime:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise DomainInvariantError("database_clock_unavailable")
        return database_time

    @staticmethod
    def _round_for_update(session: Session, round_id: UUID) -> QuizRound:
        round_ = session.scalar(select(QuizRound).where(QuizRound.id == round_id).with_for_update())
        if round_ is None:
            raise ResourceNotFoundError("round_not_found")
        return round_

    @staticmethod
    def _event_for_update(session: Session, event_id: UUID) -> Event:
        event = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
        if event is None:
            raise ResourceNotFoundError("event_not_found")
        return event

    @staticmethod
    def _required_event(session: Session, event_id: UUID) -> Event:
        event = session.get(Event, event_id)
        if event is None:
            raise ResourceNotFoundError("event_not_found")
        return event


def mutation_request_hash(
    operation: MutationOperation,
    target_id: UUID,
    request: OperatorMutationRequest,
) -> str:
    """path targetを含むcanonical JSONから冪等要求のfingerprintを作る。"""

    canonical_payload = json.dumps(
        {
            "operation": operation,
            "target_id": str(target_id),
            "request": request.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_payload.encode()).hexdigest()


def visibility_audit_state(visibility: ResultVisibility, target: VisibilityMutationTarget) -> str:
    """順位一括操作では、変更前の不一致も監査できるよう両項目を保存する。"""
    if target == "rankings":
        return visibility.model_dump_json(include={"personal_ranking", "overall_ranking"})
    return str(getattr(visibility, target)).lower()


def mutation_resulting_version(
    operation: MutationOperation,
    response: OperatorMutationResponse,
) -> int:
    """冪等記録へ操作対象entityの更新後versionを保存する。"""

    if operation in {"set_result_visibility", "set_final_round", "set_award_ceremony"}:
        return response.event_version
    if response.round_version is None:
        raise DomainInvariantError("round_mutation_resulting_version_missing")
    return response.round_version


def advisory_lock_key(account_id: UUID, idempotency_key: UUID) -> int:
    """PostgreSQL advisory lock用のsigned 64-bit keyを決定的に作る。"""

    digest = hashlib.sha256(f"{account_id}:{idempotency_key}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@lru_cache
def get_operator_mutation_service() -> OperatorMutationService:
    engine = get_engine()
    settings = get_settings()
    round_backup_service = (
        RoundBackupService(
            settings.round_backup_directory,
            settings.round_backup_encryption_key,
        )
        if settings.round_backup_storage_mode == "filesystem"
        else None
    )
    return OperatorMutationService(
        engine,
        QuizDomainService(engine),
        round_backup_service,
    )
