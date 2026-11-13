from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import SecretStr

from quiz_app.audience.contracts import TimeResponse
from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.domain.rounds import STANDARD_DURATION_SECONDS
from quiz_app.load_test.controller_contracts import (
    CheckpointSealedPayload,
    ControllerAction,
    ControllerApiMutationError,
    ControllerCheckpoint,
    ControllerContext,
    ControllerJournalRecord,
    ControllerMutationResponse,
    ControllerOperation,
    ControllerRefusal,
    ControllerStatus,
    InitializedPayload,
    MutationFailedPayload,
    MutationPlannedPayload,
    MutationSucceededPayload,
    ReceiptAcceptedPayload,
    ResultsObservedPayload,
    VerificationExpectation,
    VerificationRequestedPayload,
    VerificationRequestExpectation,
    VerificationRequestSealedPayload,
    canonical_json_bytes,
    controller_idempotency_key,
    manifest_context,
    required_confirmation_phrase,
    verification_id,
)
from quiz_app.load_test.controller_journal import (
    ControllerJournal,
    load_checkpoint,
    seal_checkpoint,
)
from quiz_app.load_test.controller_results import (
    results_deadline,
    results_observation_in_window,
    validate_results_state,
)
from quiz_app.load_test.fault_controller_contracts import fault_recovery_offset_for_round
from quiz_app.load_test.legacy_control_contracts import (
    OperatorRoundResponse,
    OperatorStateResponse,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.runtime_configuration import (
    FORMAL_RESULT_VISIBILITY,
    matches_formal_runtime_configuration,
)
from quiz_app.load_test.verification_contracts import (
    ArtifactIdentity,
    LoadedVerificationReceipt,
)
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RoundResultPublication,
    RoundState,
)
from quiz_app.operator.contracts import OperatorSessionResponse
from quiz_app.operator.control_contracts import (
    NextRoundMutationRequest,
    OperatorLatestRankingResponse,
    OperatorMutationRequest,
    ScheduleRoundMutationRequest,
)

MAX_CLOCK_OBSERVATION_GAP = timedelta(seconds=5)
SCHEDULE_TOLERANCE = timedelta(seconds=1)
ControllerPhase = Literal[
    "prepare",
    "schedule",
    "publish",
    "wait-results",
    "verification",
    "verification_request",
    "advance",
    "progression",
    "checkpoint",
    "completed",
    "pending",
]


class ControllerApi(Protocol):
    def login(self, username: str, password: SecretStr) -> OperatorSessionResponse: ...

    def get_session(self) -> OperatorSessionResponse: ...

    def get_liveness(self) -> LiveHealthResponse: ...

    def get_readiness(self) -> ReadinessHealthResponse: ...

    def get_server_time(self) -> TimeResponse: ...

    def get_event_state(self) -> OperatorStateResponse: ...

    def get_latest_ranking(self) -> OperatorLatestRankingResponse | None: ...

    def post_mutation(
        self,
        operation: ControllerOperation,
        round_id: UUID,
        body: bytes,
    ) -> ControllerMutationResponse: ...


class VerificationProvider(Protocol):
    def generator_identity(
        self,
        path: Path,
        manifest: LoadTestManifest,
        *,
        round_number: int,
        round_id: UUID,
    ) -> ArtifactIdentity: ...

    def load_receipt(
        self,
        path: Path,
        expectation: VerificationExpectation,
    ) -> LoadedVerificationReceipt: ...

    def seal_verification_request(
        self,
        directory: Path,
        generator_path: Path,
        manifest: LoadTestManifest,
        expectation: VerificationRequestExpectation,
    ) -> ArtifactIdentity: ...

    def validate_verification_request(
        self,
        directory: Path,
        artifact: ArtifactIdentity,
        manifest: LoadTestManifest,
        expectation: VerificationRequestExpectation,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _Progress:
    initialized: bool
    phase: ControllerPhase
    round_number: int
    round_id: UUID
    question_id: UUID
    pending: MutationPlannedPayload | None = None
    pending_return_phase: str | None = None
    last_plan: MutationPlannedPayload | None = None
    last_success: MutationSucceededPayload | None = None
    verification: VerificationRequestedPayload | None = None
    verification_head: str | None = None
    verification_requested_at: datetime | None = None
    verification_request_artifact: ArtifactIdentity | None = None
    receipt: ReceiptAcceptedPayload | None = None
    next_attempt: int = 1
    results: ResultsObservedPayload | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedReceiptInput:
    loaded: LoadedVerificationReceipt


def _require_isolated_directories(*directories: Path) -> None:
    try:
        resolved = tuple(directory.resolve(strict=True) for directory in directories)
    except OSError as error:
        raise ControllerRefusal("controller_artifact_directory_invalid") from error
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ControllerRefusal("controller_artifact_directories_overlap")


def _operation_for(
    action: Literal["prepare", "schedule", "publish", "advance"], round_number: int
) -> ControllerOperation:
    operations: dict[str, ControllerOperation] = {
        "prepare": "begin_preparation",
        "schedule": "schedule_round",
        "publish": "publish_round_results",
        "advance": "complete_event" if round_number == 10 else "advance_round",
    }
    return operations[action]


def _action_for(operation: ControllerOperation) -> ControllerAction:
    actions: dict[ControllerOperation, ControllerAction] = {
        "begin_preparation": "prepare",
        "schedule_round": "schedule",
        "publish_round_results": "publish",
        "advance_round": "advance",
        "complete_event": "advance",
    }
    return actions[operation]


def _operation_deadline(
    manifest: LoadTestManifest,
    round_number: int,
    operation: ControllerOperation,
) -> datetime:
    slot = manifest.scenario.rounds[round_number - 1]
    target_open_at = manifest.scheduled_start_at + timedelta(seconds=slot.start_offset_seconds)
    if operation == "begin_preparation":
        return target_open_at - timedelta(seconds=6)
    if operation == "schedule_round":
        return target_open_at - timedelta(seconds=4)
    end_offset = (
        manifest.scenario.rounds[round_number].start_offset_seconds
        if round_number < 10
        else manifest.scenario.lifecycle.measurement_end_seconds
    )
    return manifest.scheduled_start_at + timedelta(seconds=end_offset)


def _mutation_request(
    manifest: LoadTestManifest,
    *,
    round_number: int,
    operation: ControllerOperation,
    expected_version: int,
    attempt: int = 1,
) -> OperatorMutationRequest | NextRoundMutationRequest | ScheduleRoundMutationRequest:
    slot = manifest.scenario.rounds[round_number - 1]
    target_open_at = manifest.scheduled_start_at + timedelta(seconds=slot.start_offset_seconds)
    execute_not_after = _operation_deadline(manifest, round_number, operation)
    common = {
        "expected_version": expected_version,
        "idempotency_key": controller_idempotency_key(
            manifest.run_id,
            round_number,
            operation,
            attempt,
        ),
        "execute_not_after": execute_not_after,
    }
    if operation == "schedule_round":
        return ScheduleRoundMutationRequest(**common, target_open_at=target_open_at)
    if operation == "advance_round":
        return NextRoundMutationRequest(
            **common,
            question_id=manifest.questions[round_number].id,
        )
    return OperatorMutationRequest(**common)


def _request_body(
    request: OperatorMutationRequest | NextRoundMutationRequest | ScheduleRoundMutationRequest,
) -> bytes:
    # HTTP adapterも同じPydantic contractのfield順でserializeする。
    return request.model_dump_json().encode("utf-8")


def _validate_response(
    context: ControllerContext,
    plan: MutationPlannedPayload,
    response: ControllerMutationResponse,
) -> None:
    expected_states: dict[ControllerOperation, RoundState] = {
        "begin_preparation": RoundState.PREPARING_ASSETS,
        "schedule_round": RoundState.SCHEDULED,
        "publish_round_results": RoundState.REVEAL,
        "advance_round": RoundState.PREPARING_ASSETS,
        "complete_event": RoundState.ROUND_BACKUP_VALIDATED,
    }
    if (
        response.operation != plan.operation
        or response.event_id != context.event_id
        or response.round_id is None
        or response.round_version is None
        or response.round_state is not expected_states[plan.operation]
        or response.result_visibility != FORMAL_RESULT_VISIBILITY
    ):
        raise ControllerRefusal("controller_mutation_response_drift")
    if plan.operation == "advance_round":
        if response.round_id == plan.round_id:
            raise ControllerRefusal("controller_mutation_response_drift")
    elif response.round_id != plan.round_id:
        raise ControllerRefusal("controller_mutation_response_drift")


def _checkpoint_contract(
    context: ControllerContext,
    *,
    round_number: int,
    round_id: UUID,
    question_id: UUID,
    journal_head: str,
    plan: MutationPlannedPayload,
    success: MutationSucceededPayload,
    verification: VerificationRequestedPayload,
    verification_head: str,
    verification_request_artifact: ArtifactIdentity,
    receipt: ReceiptAcceptedPayload,
    sealed_at: datetime,
) -> ControllerCheckpoint:
    if success.response.operation not in {"advance_round", "complete_event"}:
        raise ControllerRefusal("controller_journal_state_invalid")
    return ControllerCheckpoint(
        format="load-test-controller-checkpoint-v2",
        run_id=context.run_id,
        event_id=context.event_id,
        source_commit=context.source_commit,
        schema_revision=context.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=round_number,
        round_id=round_id,
        question_id=question_id,
        journal_head_sha256=journal_head,
        verification_requested_head_sha256=verification_head,
        verification_request_artifact=verification_request_artifact,
        receipt_artifact=receipt.receipt_artifact,
        verification_id=receipt.verification_id,
        report=receipt.report,
        generator_evidence=verification.generator_evidence,
        completed_operation=cast(
            "Literal['advance_round','complete_event']",
            success.response.operation,
        ),
        idempotency_key=plan.idempotency_key,
        response_sha256=sha256(canonical_json_bytes(success.response)).hexdigest(),
        next_round_id=(
            success.response.round_id if success.response.operation == "advance_round" else None
        ),
        sealed_at=sealed_at,
    )


def _verification_request_expectation(
    context: ControllerContext,
    *,
    round_number: int,
    round_id: UUID,
    question_id: UUID,
    verification: VerificationRequestedPayload,
    verification_head: str,
    requested_at: datetime,
) -> VerificationRequestExpectation:
    return VerificationRequestExpectation(
        **context.model_dump(
            include={
                "run_id",
                "event_id",
                "source_commit",
                "schema_revision",
                "manifest_sha256",
                "scenario_sha256",
            }
        ),
        round_number=round_number,
        round_id=round_id,
        question_id=question_id,
        verification_requested_head_sha256=verification_head,
        generator_evidence=verification.generator_evidence,
        requested_at=requested_at,
    )


def _journal_progress(
    manifest: LoadTestManifest,
    context: ControllerContext,
    records: list[ControllerJournalRecord],
    checkpoint_validator: Callable[[ControllerCheckpoint, ArtifactIdentity], None] | None = None,
    verification_request_validator: (
        Callable[[VerificationRequestExpectation, ArtifactIdentity], None] | None
    ) = None,
) -> _Progress:
    round_number = 1
    round_id = manifest.initial_round.id
    question_id = manifest.questions[0].id
    if not records:
        return _Progress(False, "prepare", round_number, round_id, question_id)
    first = records[0].payload
    if (
        not isinstance(first, InitializedPayload)
        or first.context != context
        or first.round_version != manifest.initial_round.version
        or records[0].run_id != context.run_id
        or records[0].event_id != context.event_id
    ):
        raise ControllerRefusal("controller_journal_context_mismatch")
    phase: str = "prepare"
    pending: MutationPlannedPayload | None = None
    pending_return: str | None = None
    last_plan: MutationPlannedPayload | None = None
    last_success: MutationSucceededPayload | None = None
    verification: VerificationRequestedPayload | None = None
    verification_head: str | None = None
    verification_requested_at: datetime | None = None
    verification_request_artifact: ArtifactIdentity | None = None
    receipt: ReceiptAcceptedPayload | None = None
    next_attempt = 1
    results: ResultsObservedPayload | None = None

    for record in records[1:]:
        payload = record.payload
        if isinstance(payload, MutationPlannedPayload):
            if phase not in {"prepare", "schedule", "publish", "progression"}:
                raise ControllerRefusal("controller_journal_state_invalid")
            action = (
                "advance"
                if phase == "progression"
                else cast("Literal['prepare', 'schedule', 'publish']", phase)
            )
            expected_operation = _operation_for(action, round_number)
            request = _mutation_request(
                manifest,
                round_number=round_number,
                operation=expected_operation,
                expected_version=payload.expected_version,
                attempt=payload.attempt,
            )
            if (
                payload.round_number != round_number
                or payload.round_id != round_id
                or payload.question_id != question_id
                or payload.operation != expected_operation
                or payload.attempt != next_attempt
                or payload.idempotency_key != request.idempotency_key
                or payload.request_sha256 != sha256(_request_body(request)).hexdigest()
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            pending, last_plan, pending_return = payload, payload, phase
            phase = "pending"
            continue
        if isinstance(payload, MutationSucceededPayload):
            if pending is None or phase != "pending":
                raise ControllerRefusal("controller_journal_state_invalid")
            if (
                payload.round_number != round_number
                or payload.target_round_id != round_id
                or payload.idempotency_key != pending.idempotency_key
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            _validate_response(context, pending, payload.response)
            last_success = payload
            operation = pending.operation
            pending = None
            phase = {
                "begin_preparation": "schedule",
                "schedule_round": "publish",
                "publish_round_results": "wait-results",
                "advance_round": "checkpoint",
                "complete_event": "checkpoint",
            }[operation]
            next_attempt = 1
            continue
        if isinstance(payload, MutationFailedPayload):
            if (
                pending is None
                or phase != "pending"
                or pending.operation not in {"advance_round", "complete_event"}
                or payload.round_number != round_number
                or payload.target_round_id != round_id
                or payload.operation != pending.operation
                or payload.attempt != pending.attempt
                or payload.idempotency_key != pending.idempotency_key
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            next_attempt = payload.attempt + 1
            if next_attempt > 10:
                raise ControllerRefusal("controller_mutation_attempts_exhausted")
            pending = None
            phase = "progression"
            continue
        if isinstance(payload, ResultsObservedPayload):
            if (
                phase != "wait-results"
                or last_success is None
                or last_success.response.operation != "publish_round_results"
                or last_success.response.round_version is None
                or payload.round_number != round_number
                or payload.round_id != round_id
                or payload.question_id != question_id
                or payload.round_version != last_success.response.round_version
                or payload.event_version != last_success.response.event_version
                or payload.ranking_snapshot_version != round_number
                or not results_observation_in_window(manifest, round_number, record.recorded_at)
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            results = payload
            phase = "verification"
            continue
        if isinstance(payload, VerificationRequestedPayload):
            if (
                phase != "verification"
                or results is None
                or payload.round_number != round_number
                or payload.round_id != round_id
                or payload.question_id != question_id
                or payload.generator_evidence != results.generator_evidence
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            verification = payload
            verification_head = record.entry_sha256
            verification_requested_at = record.recorded_at
            phase = "verification_request"
            continue
        if isinstance(payload, VerificationRequestSealedPayload):
            if (
                phase != "verification_request"
                or verification is None
                or verification_head is None
                or verification_requested_at is None
                or payload.round_number != round_number
                or payload.round_id != round_id
                or payload.verification_requested_head_sha256 != verification_head
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            expectation = _verification_request_expectation(
                context,
                round_number=round_number,
                round_id=round_id,
                question_id=question_id,
                verification=verification,
                verification_head=verification_head,
                requested_at=verification_requested_at,
            )
            if verification_request_validator is not None:
                verification_request_validator(expectation, payload.request_artifact)
            verification_request_artifact = payload.request_artifact
            phase = "advance"
            continue
        if isinstance(payload, ReceiptAcceptedPayload):
            if (
                phase != "advance"
                or verification_head is None
                or payload.round_number != round_number
                or payload.round_id != round_id
                or payload.verification_requested_head_sha256 != verification_head
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            receipt, phase = payload, "progression"
            continue
        if isinstance(payload, CheckpointSealedPayload):
            if (
                phase != "checkpoint"
                or payload.round_number != round_number
                or last_success is None
                or last_plan is None
                or verification is None
                or verification_head is None
                or verification_request_artifact is None
                or receipt is None
                or record.previous_sha256 is None
            ):
                raise ControllerRefusal("controller_journal_state_invalid")
            expected_checkpoint = _checkpoint_contract(
                context,
                round_number=round_number,
                round_id=round_id,
                question_id=question_id,
                journal_head=record.previous_sha256,
                plan=last_plan,
                success=last_success,
                verification=verification,
                verification_head=verification_head,
                verification_request_artifact=verification_request_artifact,
                receipt=receipt,
                sealed_at=records[record.sequence - 2].recorded_at,
            )
            if checkpoint_validator is not None:
                checkpoint_validator(expected_checkpoint, payload.checkpoint)
            if last_success.response.operation == "complete_event":
                phase = "completed"
                continue
            round_number += 1
            if round_number > 10 or last_success.response.round_id is None:
                raise ControllerRefusal("controller_journal_state_invalid")
            round_id = last_success.response.round_id
            question_id = manifest.questions[round_number - 1].id
            phase = "prepare"
            last_plan = verification = receipt = None
            verification_head = None
            verification_requested_at = None
            verification_request_artifact = None
            next_attempt = 1
            results = None
            continue
        raise ControllerRefusal("controller_journal_state_invalid")

    return _Progress(
        True,
        cast(ControllerPhase, phase),
        round_number,
        round_id,
        question_id,
        pending,
        pending_return,
        last_plan,
        last_success,
        verification,
        verification_head,
        verification_requested_at,
        verification_request_artifact,
        receipt,
        next_attempt,
        results,
    )


class LoadTestController:
    """1回の明示操作だけを実行し、外部待機を跨いで安全にresumeする。"""

    def __init__(
        self,
        manifest: LoadTestManifest,
        journal: ControllerJournal,
        checkpoint_directory: Path,
        verification_request_directory: Path,
        repository_root: Path,
        api: ControllerApi,
        verification_provider: VerificationProvider,
    ) -> None:
        self._manifest = manifest
        self._context = manifest_context(manifest)
        self._journal = journal
        self._checkpoint_directory = checkpoint_directory
        self._verification_request_directory = verification_request_directory
        self._repository_root = repository_root
        self._api = api
        self._verification_provider = verification_provider
        _require_isolated_directories(
            journal.path.parent,
            checkpoint_directory,
            verification_request_directory,
        )

    def confirmation_prompt(
        self,
        action: ControllerAction,
        *,
        receipt_path: Path | None = None,
    ) -> str:
        """現在のjournal/receiptにだけ一致する承認文を返す。"""

        progress = self._recover_local_state()
        if action != self._expected_action(progress):
            raise ControllerRefusal("controller_action_out_of_order")
        if action == "advance" and progress.phase == "advance":
            receipt = self._load_and_validate_receipt(progress, receipt_path)
            receipt_sha256 = receipt.loaded.artifact.sha256
        elif action == "advance" and progress.receipt is not None:
            receipt_sha256 = progress.receipt.receipt_artifact.sha256
        else:
            receipt_sha256 = None
        return required_confirmation_phrase(
            self._context,
            progress.round_number,
            action,
            receipt_sha256,
        )

    def execute(
        self,
        action: ControllerAction,
        *,
        username: str,
        password: SecretStr,
        confirmation_phrase: str,
        generator_evidence_path: Path | None = None,
        receipt_path: Path | None = None,
    ) -> ControllerStatus:
        progress = self._recover_local_state()
        expected_action = self._expected_action(progress)
        if action != expected_action:
            raise ControllerRefusal("controller_action_out_of_order")
        verified_receipt = (
            self._load_and_validate_receipt(progress, receipt_path)
            if action == "advance" and progress.phase == "advance"
            else None
        )
        receipt_sha256 = (
            verified_receipt.loaded.artifact.sha256
            if verified_receipt is not None
            else progress.receipt.receipt_artifact.sha256
            if action == "advance" and progress.receipt is not None
            else None
        )
        expected_phrase = required_confirmation_phrase(
            self._context,
            progress.round_number,
            action,
            receipt_sha256,
        )
        if confirmation_phrase != expected_phrase:
            raise ControllerRefusal("controller_confirmation_mismatch")

        session = self._api.login(username, password)
        if (
            self._api.get_session() != session
            or session.operator.role is not AdminRole.OPERATOR
            or session.operator.username != username.strip()
        ):
            raise ControllerRefusal("controller_operator_session_drift")
        if progress.initialized:
            initialized = self._journal.records[0].payload
            if (
                not isinstance(initialized, InitializedPayload)
                or initialized.operator_account_id != session.operator.account_id
            ):
                raise ControllerRefusal("controller_operator_identity_drift")
        self._validate_health(progress)
        if progress.phase == "pending":
            self._retry_pending(progress)
            current = self._recover_local_state()
            self._validate_after_step(current, allow_fault_wait=action == "wait-results")
            return self._status(current)
        server_time, state = self._read_live_state(progress)
        if not progress.initialized:
            self._journal.append(
                run_id=self._context.run_id,
                event_id=self._context.event_id,
                payload=InitializedPayload(
                    kind="initialized",
                    context=self._context,
                    operator_account_id=session.operator.account_id,
                    server_time=server_time.server_time,
                    event_version=state.event_version,
                    round_version=state.current_round.version,
                ),
            )
            progress = _journal_progress(
                self._manifest,
                self._context,
                self._journal.records,
                self._validate_checkpoint,
                self._validate_verification_request,
            )

        if action == "advance":
            self._advance(progress, verified_receipt, server_time, state)
        elif action == "wait-results":
            if progress.phase == "wait-results":
                if not self._observe_results(progress, state, server_time, generator_evidence_path):
                    return self._status(progress)
                progress = self._recover_local_state()
            self._finalize_verification_request(progress, generator_evidence_path)
        else:
            self._perform_regular(action, progress, state, server_time)
        current = self._recover_local_state()
        self._validate_after_step(current, allow_fault_wait=action == "wait-results")
        return self._status(current)

    def _recover_local_state(self) -> _Progress:
        progress = _journal_progress(
            self._manifest,
            self._context,
            self._journal.records,
            self._validate_checkpoint,
            self._validate_verification_request,
        )
        while progress.phase == "checkpoint":
            self._seal_progress_checkpoint(progress)
            progress = _journal_progress(
                self._manifest,
                self._context,
                self._journal.records,
                self._validate_checkpoint,
                self._validate_verification_request,
            )
        return progress

    @staticmethod
    def _expected_action(progress: _Progress) -> ControllerAction:
        if progress.phase == "pending":
            if progress.pending is None:
                raise ControllerRefusal("controller_journal_state_invalid")
            return _action_for(progress.pending.operation)
        if progress.phase == "progression":
            return "advance"
        if progress.phase in {"verification", "verification_request"}:
            return "wait-results"
        if progress.phase == "completed":
            raise ControllerRefusal("controller_already_completed")
        return cast(ControllerAction, progress.phase)

    def _validate_health(self, progress: _Progress, *, allow_fault_wait: bool = False) -> None:
        liveness = self._api.get_liveness()
        readiness = self._api.get_readiness()
        redis_fault_slot = progress.round_number == 5 and (
            progress.phase
            in {
                "prepare",
                "schedule",
                "publish",
                "wait-results",
                "verification",
                "verification_request",
            }
            # 結果の読取りでadvance待ちへ移った直後だけ許容し、実mutation前は許容しない。
            or (allow_fault_wait and progress.phase == "advance")
        )
        redis_is_valid = (
            readiness.redis in {"ok", "degraded"}
            and readiness.status == ("ready" if readiness.redis == "ok" else "degraded")
            if redis_fault_slot
            else readiness.redis == "ok" and readiness.status == "ready"
        )
        if (
            liveness.version != self._context.source_commit
            or readiness.database != "ok"
            or readiness.migration != "current"
            or not redis_is_valid
        ):
            raise ControllerRefusal("controller_health_preflight_failed")

    def _validate_after_step(self, progress: _Progress, *, allow_fault_wait: bool = False) -> None:
        self._validate_health(progress, allow_fault_wait=allow_fault_wait)
        if progress.phase == "completed":
            state = self._api.get_event_state()
            current = state.current_round
            if (
                state.event_id != self._context.event_id
                or state.event_state is not EventState.COMPLETED
                or not matches_formal_runtime_configuration(state)
                or tuple(state.used_question_ids)
                != tuple(question.id for question in self._manifest.questions)
                or current is None
                or current.round_id != progress.round_id
                or current.state is not RoundState.ROUND_BACKUP_VALIDATED
                or current.result_publication is not RoundResultPublication.PUBLISHED
                or not self._versions_match(progress, state, current)
            ):
                raise ControllerRefusal("controller_completion_drift")
            return
        self._read_live_state(progress)

    def _read_live_state(self, progress: _Progress) -> tuple[TimeResponse, OperatorStateResponse]:
        for attempt in range(2):
            server_time = self._api.get_server_time()
            state = self._api.get_event_state()
            try:
                self._validate_common_state(progress, server_time, state)
                if progress.phase in {"wait-results", "verification", "verification_request"}:
                    validate_results_state(
                        self._manifest, progress.round_number, server_time, state
                    )
            except ControllerRefusal as error:
                # projectionの複数SELECT間で自動集計がcommitした場合だけ1回再取得する。
                if (
                    attempt == 0
                    and str(error)
                    in {"controller_live_state_drift", "controller_results_ranking_mismatch"}
                    and self._may_cross_automatic_aggregation(progress, state)
                ):
                    continue
                raise
            return server_time, state
        raise ControllerRefusal("controller_live_state_drift")

    @staticmethod
    def _may_cross_automatic_aggregation(progress: _Progress, state: OperatorStateResponse) -> bool:
        success = progress.last_success
        current = state.current_round
        if (
            progress.phase != "publish"
            or success is None
            or success.response.operation != "schedule_round"
            or success.response.round_version is None
            or current is None
            or current.round_id != progress.round_id
        ):
            return False
        response = success.response
        return (
            current.version in {response.round_version, response.round_version + 1}
            and state.event_version in {response.event_version, response.event_version + 1}
            and state.latest_ranking_snapshot_version
            in {progress.round_number - 1 or None, progress.round_number}
        )

    def _validate_common_state(
        self,
        progress: _Progress,
        server_time: TimeResponse,
        state: OperatorStateResponse,
    ) -> None:
        expected_event_state = (
            EventState.LOBBY_OPEN
            if progress.round_number == 1 and progress.phase in {"prepare", "schedule"}
            else EventState.IN_PROGRESS
        )
        questions = tuple(
            (
                question.question_id,
                question.version,
                question.display_order,
                question.prompt,
                tuple(
                    (option.option_id, option.label, option.content, option.is_correct)
                    for option in question.options
                ),
            )
            for question in state.available_questions
        )
        expected_questions = tuple(
            (
                question.id,
                question.version,
                question.display_order,
                question.prompt,
                tuple(
                    (
                        option.id,
                        option.label,
                        option.content,
                        option.label == "B",
                    )
                    for option in question.options
                ),
            )
            for question in self._manifest.questions
        )
        current = state.current_round
        if current is not None and (
            current.state is RoundState.VOID_CANDIDATE or current.result_failure_code is not None
        ):
            raise ControllerRefusal("controller_automatic_results_failed")
        if (
            state.event_id != self._context.event_id
            or state.event_title != self._manifest.event.title
            or state.event_state is not expected_event_state
            or not matches_formal_runtime_configuration(state)
            or state.participant_count != self._manifest.scenario.lifecycle.target_total_sessions
            or questions != expected_questions
            or tuple(state.used_question_ids)
            != tuple(question.id for question in self._manifest.questions[: progress.round_number])
            or current is None
            or current.round_id != progress.round_id
            or current.question.question_id != progress.question_id
            or not self._versions_match(progress, state, current)
            or not self._schedule_times_match(progress, current)
            or state.projected_at < server_time.server_time
            or state.projected_at - server_time.server_time > MAX_CLOCK_OBSERVATION_GAP
        ):
            raise ControllerRefusal("controller_live_state_drift")

    def _versions_match(
        self,
        progress: _Progress,
        state: OperatorStateResponse,
        current: OperatorRoundResponse,
    ) -> bool:
        if not progress.initialized:
            return (
                current.version == self._manifest.initial_round.version and state.event_version == 1
            )
        initialized = self._journal.records[0].payload
        if not isinstance(initialized, InitializedPayload):
            return False
        success = progress.last_success
        if success is None:
            return (
                current.version == initialized.round_version
                and state.event_version == initialized.event_version
            )
        response = success.response
        if response.round_version is None:
            return False
        if response.operation == "begin_preparation":
            exact = (
                current.state is RoundState.PREPARING_ASSETS
                and current.version == response.round_version
                and state.event_version == response.event_version
            )
            readiness_transition = (
                current.state is RoundState.READY_TO_SCHEDULE
                and current.version == response.round_version + 1
                and state.event_version == response.event_version + 1
            )
            return exact or readiness_transition
        if response.operation == "schedule_round":
            if progress.phase != "publish":
                return False
            aggregated = current.state is RoundState.READY_TO_REVEAL
            return (
                current.result_publication is RoundResultPublication.WITHHELD
                and current.version == response.round_version + int(aggregated)
                and state.event_version == response.event_version + int(aggregated)
                and state.latest_ranking_snapshot_version
                == (progress.round_number if aggregated else progress.round_number - 1 or None)
            )
        if response.operation == "publish_round_results":
            observed = progress.results
            completed_failures = progress.next_attempt - 1
            if observed is not None and progress.phase == "progression" and completed_failures > 0:
                return (
                    current.state is RoundState.ROUND_BACKUP_FAILED
                    and current.version == observed.round_version + 2 + (completed_failures - 1) * 3
                    and state.event_version == observed.event_version + completed_failures
                )
            return (
                current.state is RoundState.REVEAL
                and current.result_publication is RoundResultPublication.PUBLISHED
                and current.version == response.round_version
                and state.event_version == response.event_version
                and state.latest_ranking_snapshot_version == progress.round_number
            )
        return (
            current.version == response.round_version
            and state.event_version == response.event_version
        )

    def _schedule_times_match(
        self,
        progress: _Progress,
        current: OperatorRoundResponse,
    ) -> bool:
        success = progress.last_success
        if success is None or success.response.operation not in {
            "schedule_round",
            "publish_round_results",
        }:
            return True
        slot = self._manifest.scenario.rounds[progress.round_number - 1]
        target = self._manifest.scheduled_start_at + timedelta(seconds=slot.start_offset_seconds)
        return current.open_at == target and current.standard_close_at == target + timedelta(
            seconds=STANDARD_DURATION_SECONDS
        )

    def _perform_regular(
        self,
        action: Literal["prepare", "schedule", "publish"],
        progress: _Progress,
        state: OperatorStateResponse,
        server_time: TimeResponse,
    ) -> None:
        current = state.current_round
        if current is None:
            raise ControllerRefusal("controller_live_state_drift")
        if action == "prepare":
            slot = self._manifest.scenario.rounds[progress.round_number - 1]
            prepare_deadline = (
                self._manifest.scheduled_start_at
                + timedelta(seconds=slot.start_offset_seconds - 5)
                - SCHEDULE_TOLERANCE
            )
            if current.state is not RoundState.PREPARING_ASSETS:
                raise ControllerRefusal("controller_live_state_drift")
            if server_time.server_time >= prepare_deadline:
                raise ControllerRefusal("controller_prepare_deadline_missed")
            if progress.round_number == 1 and server_time.server_time < (
                self._manifest.scheduled_start_at
                + timedelta(seconds=self._manifest.scenario.lifecycle.ramp_end_seconds)
            ):
                raise ControllerRefusal("controller_ramp_up_not_completed")
        elif action == "schedule":
            self._validate_schedule_gate(progress.round_number, server_time, current)
        elif action == "publish":
            if current.state is not RoundState.READY_TO_REVEAL or not current.can_publish_results:
                if max(server_time.server_time, state.projected_at) >= results_deadline(
                    self._manifest, progress.round_number
                ):
                    raise ControllerRefusal("controller_results_deadline_missed")
                return
        self._plan_and_post(
            progress, _operation_for(action, progress.round_number), current.version
        )

    def _observe_results(
        self,
        progress: _Progress,
        state: OperatorStateResponse,
        server_time: TimeResponse,
        generator_evidence_path: Path | None,
    ) -> bool:
        if not validate_results_state(self._manifest, progress.round_number, server_time, state):
            return False
        if generator_evidence_path is None:
            raise ControllerRefusal("controller_generator_evidence_required")
        _require_isolated_directories(
            self._journal.path.parent,
            self._checkpoint_directory,
            self._verification_request_directory,
            generator_evidence_path.parent,
        )
        generator = self._verification_provider.generator_identity(
            generator_evidence_path,
            self._manifest,
            round_number=progress.round_number,
            round_id=progress.round_id,
        )
        ranking = self._api.get_latest_ranking()
        # evidence検証とranking取得を跨いだ外部進行や期限超過を、seal前に再確認する。
        now, latest = self._read_live_state(progress)
        if not validate_results_state(self._manifest, progress.round_number, now, latest):
            raise ControllerRefusal("controller_results_state_drift")
        current = latest.current_round
        if (
            current is None
            or ranking is None
            or ranking.round_id != progress.round_id
            or ranking.snapshot_version != progress.round_number
            or ranking.snapshot_version != latest.latest_ranking_snapshot_version
            or len(ranking.rows) != (500 if progress.round_number <= 2 else 502)
            or len({row.public_label for row in ranking.rows}) != len(ranking.rows)
        ):
            raise ControllerRefusal("controller_results_ranking_mismatch")
        observed_at = self._api.get_server_time().server_time
        if observed_at < latest.projected_at:
            raise ControllerRefusal("controller_live_state_drift")
        if not results_observation_in_window(self._manifest, progress.round_number, observed_at):
            raise ControllerRefusal("controller_results_deadline_missed")
        self._journal.append(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            payload=ResultsObservedPayload(
                kind="results_observed",
                round_number=progress.round_number,
                round_id=progress.round_id,
                question_id=progress.question_id,
                event_version=latest.event_version,
                round_version=current.version,
                ranking_snapshot_version=ranking.snapshot_version,
                generator_evidence=generator,
            ),
            recorded_at=observed_at,
        )
        return True

    def _finalize_verification_request(
        self,
        progress: _Progress,
        generator_evidence_path: Path | None,
    ) -> None:
        if generator_evidence_path is None:
            raise ControllerRefusal("controller_generator_evidence_required")
        _require_isolated_directories(
            self._journal.path.parent,
            self._checkpoint_directory,
            self._verification_request_directory,
            generator_evidence_path.parent,
        )
        if progress.phase == "verification":
            observed = progress.results
            if observed is None:
                raise ControllerRefusal("controller_journal_state_invalid")
            generator = self._verification_provider.generator_identity(
                generator_evidence_path,
                self._manifest,
                round_number=progress.round_number,
                round_id=progress.round_id,
            )
            if generator != observed.generator_evidence:
                raise ControllerRefusal("controller_generator_evidence_changed")
            request_time = self._api.get_server_time().server_time
            request_deadline = results_deadline(self._manifest, progress.round_number)
            if request_time >= request_deadline:
                raise ControllerRefusal("controller_verification_request_deadline_missed")
            self._journal.append(
                run_id=self._context.run_id,
                event_id=self._context.event_id,
                payload=VerificationRequestedPayload(
                    kind="verification_requested",
                    round_number=progress.round_number,
                    round_id=progress.round_id,
                    question_id=progress.question_id,
                    generator_evidence=generator,
                ),
                recorded_at=request_time,
            )
            progress = _journal_progress(
                self._manifest,
                self._context,
                self._journal.records,
                self._validate_checkpoint,
                self._validate_verification_request,
            )
        if (
            progress.phase != "verification_request"
            or progress.verification is None
            or progress.verification_head is None
            or progress.verification_requested_at is None
        ):
            raise ControllerRefusal("controller_journal_state_invalid")
        expectation = _verification_request_expectation(
            self._context,
            round_number=progress.round_number,
            round_id=progress.round_id,
            question_id=progress.question_id,
            verification=progress.verification,
            verification_head=progress.verification_head,
            requested_at=progress.verification_requested_at,
        )
        artifact = self._verification_provider.seal_verification_request(
            self._verification_request_directory,
            generator_evidence_path,
            self._manifest,
            expectation,
        )
        self._verification_provider.validate_verification_request(
            self._verification_request_directory,
            artifact,
            self._manifest,
            expectation,
        )
        if self._api.get_server_time().server_time >= results_deadline(
            self._manifest, progress.round_number
        ):
            raise ControllerRefusal("controller_verification_request_deadline_missed")
        self._journal.append(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            payload=VerificationRequestSealedPayload(
                kind="verification_request_sealed",
                round_number=progress.round_number,
                round_id=progress.round_id,
                verification_requested_head_sha256=progress.verification_head,
                request_artifact=artifact,
            ),
        )

    def _validate_schedule_gate(
        self,
        round_number: int,
        server_time: TimeResponse,
        round_state: OperatorRoundResponse,
    ) -> None:
        slot = self._manifest.scenario.rounds[round_number - 1]
        schedule_at = self._manifest.scheduled_start_at + timedelta(
            seconds=slot.start_offset_seconds - 5
        )
        if (
            round_state.state is not RoundState.READY_TO_SCHEDULE
            or not round_state.gate.can_schedule
            or round_state.gate.blocking_reasons
            or round_state.gate.target_count != self._manifest.scenario.thresholds.target_count
            or round_state.gate.ready_count < self._manifest.scenario.thresholds.ready_count_minimum
            or round_state.gate.asset_failure_count
            > self._manifest.scenario.thresholds.asset_failure_maximum
            or not schedule_at - SCHEDULE_TOLERANCE
            <= server_time.server_time
            <= schedule_at + SCHEDULE_TOLERANCE
        ):
            raise ControllerRefusal("controller_schedule_gate_closed")

    def _plan_and_post(
        self,
        progress: _Progress,
        operation: ControllerOperation,
        expected_version: int,
        attempt: int = 1,
    ) -> None:
        self._validate_pre_dispatch_time(progress.round_number, operation)
        request = _mutation_request(
            self._manifest,
            round_number=progress.round_number,
            operation=operation,
            expected_version=expected_version,
            attempt=attempt,
        )
        body = _request_body(request)
        plan = MutationPlannedPayload(
            kind="mutation_planned",
            round_number=progress.round_number,
            round_id=progress.round_id,
            question_id=progress.question_id,
            operation=operation,
            attempt=attempt,
            expected_version=expected_version,
            idempotency_key=request.idempotency_key,
            request_sha256=sha256(body).hexdigest(),
        )
        self._journal.append(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            payload=plan,
        )
        self._post_planned(plan, body)

    def _validate_pre_dispatch_time(
        self,
        round_number: int,
        operation: ControllerOperation,
    ) -> None:
        now = self._api.get_server_time().server_time
        deadline = _operation_deadline(self._manifest, round_number, operation)
        if now >= deadline:
            raise ControllerRefusal("controller_mutation_dispatch_deadline_missed")
        slot = self._manifest.scenario.rounds[round_number - 1]
        target_open_at = self._manifest.scheduled_start_at + timedelta(
            seconds=slot.start_offset_seconds
        )
        if operation == "schedule_round" and now < target_open_at - timedelta(seconds=6):
            raise ControllerRefusal("controller_mutation_dispatch_too_early")
        recovery_offset = fault_recovery_offset_for_round(self._manifest, round_number)
        if (
            operation in {"advance_round", "complete_event"}
            and recovery_offset is not None
            and now < self._manifest.scheduled_start_at + timedelta(seconds=recovery_offset)
        ):
            raise ControllerRefusal("controller_fault_recovery_not_reached")

    def _post_planned(self, plan: MutationPlannedPayload, body: bytes) -> None:
        request = _mutation_request(
            self._manifest,
            round_number=plan.round_number,
            operation=plan.operation,
            expected_version=plan.expected_version,
            attempt=plan.attempt,
        )
        if _request_body(request) != body:
            raise ControllerRefusal("controller_journal_state_invalid")
        try:
            response = self._api.post_mutation(plan.operation, plan.round_id, body)
        except ControllerApiMutationError as error:
            if error.code != "round_backup_failed" or plan.operation not in {
                "advance_round",
                "complete_event",
            }:
                raise
            self._journal.append(
                run_id=self._context.run_id,
                event_id=self._context.event_id,
                payload=MutationFailedPayload(
                    kind="mutation_failed",
                    round_number=plan.round_number,
                    target_round_id=plan.round_id,
                    operation=plan.operation,
                    attempt=plan.attempt,
                    idempotency_key=plan.idempotency_key,
                    error_code="round_backup_failed",
                ),
            )
            raise ControllerRefusal("controller_round_backup_failed") from error
        _validate_response(self._context, plan, response)
        if plan.operation == "schedule_round":
            slot = self._manifest.scenario.rounds[plan.round_number - 1]
            target = self._manifest.scheduled_start_at + timedelta(
                seconds=slot.start_offset_seconds
            )
            if (
                response.open_at is None
                or response.standard_close_at is None
                or response.open_at != target
                or response.standard_close_at - response.open_at
                != timedelta(seconds=STANDARD_DURATION_SECONDS)
            ):
                raise ControllerRefusal("controller_schedule_response_drift")
        self._journal.append(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            payload=MutationSucceededPayload(
                kind="mutation_succeeded",
                round_number=plan.round_number,
                target_round_id=plan.round_id,
                idempotency_key=plan.idempotency_key,
                response=response,
            ),
        )

    def _retry_pending(self, progress: _Progress) -> None:
        plan = progress.pending
        if plan is None:
            raise ControllerRefusal("controller_journal_state_invalid")
        request = _mutation_request(
            self._manifest,
            round_number=plan.round_number,
            operation=plan.operation,
            expected_version=plan.expected_version,
            attempt=plan.attempt,
        )
        body = _request_body(request)
        if sha256(body).hexdigest() != plan.request_sha256:
            raise ControllerRefusal("controller_journal_state_invalid")
        self._post_planned(plan, body)

    def _advance(
        self,
        progress: _Progress,
        verified_receipt: _VerifiedReceiptInput | None,
        server_time: TimeResponse,
        state: OperatorStateResponse,
    ) -> None:
        end_offset = (
            self._manifest.scenario.rounds[progress.round_number].start_offset_seconds
            if progress.round_number < 10
            else self._manifest.scenario.lifecycle.measurement_end_seconds
        )
        if server_time.server_time >= self._manifest.scheduled_start_at + timedelta(
            seconds=end_offset
        ):
            raise ControllerRefusal("controller_progression_deadline_missed")
        recovery_offset = fault_recovery_offset_for_round(
            self._manifest,
            progress.round_number,
        )
        if recovery_offset is not None and server_time.server_time < (
            self._manifest.scheduled_start_at + timedelta(seconds=recovery_offset)
        ):
            raise ControllerRefusal("controller_fault_recovery_not_reached")
        if progress.phase == "progression":
            if progress.receipt is None:
                raise ControllerRefusal("controller_journal_state_invalid")
            current = state.current_round
            if current is None or current.state not in {
                RoundState.REVEAL,
                RoundState.ROUND_BACKUP_FAILED,
            }:
                raise ControllerRefusal("controller_live_state_drift")
            self._plan_and_post(
                progress,
                _operation_for("advance", progress.round_number),
                current.version,
                attempt=progress.next_attempt,
            )
            return
        if verified_receipt is None or progress.verification_head is None:
            raise ControllerRefusal("controller_verification_receipt_required")
        loaded = verified_receipt.loaded
        receipt = loaded.receipt
        current = state.current_round
        if (
            current is None
            or current.state is not RoundState.REVEAL
            or current.result_publication is not RoundResultPublication.PUBLISHED
        ):
            raise ControllerRefusal("controller_live_state_drift")
        self._journal.append(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            payload=ReceiptAcceptedPayload(
                kind="receipt_accepted",
                round_number=progress.round_number,
                round_id=progress.round_id,
                verification_id=receipt.verification_id,
                receipt_artifact=loaded.artifact,
                report=receipt.report,
                verification_requested_head_sha256=progress.verification_head,
            ),
        )
        advanced = _journal_progress(
            self._manifest,
            self._context,
            self._journal.records,
            self._validate_checkpoint,
            self._validate_verification_request,
        )
        self._plan_and_post(
            advanced,
            _operation_for("advance", progress.round_number),
            current.version,
            attempt=advanced.next_attempt,
        )

    def _load_and_validate_receipt(
        self,
        progress: _Progress,
        receipt_path: Path | None,
    ) -> _VerifiedReceiptInput:
        if (
            receipt_path is None
            or progress.verification is None
            or progress.verification_head is None
        ):
            raise ControllerRefusal("controller_verification_receipt_required")
        _require_isolated_directories(
            self._journal.path.parent,
            self._checkpoint_directory,
            self._verification_request_directory,
            receipt_path.parent,
        )
        expectation = VerificationExpectation(
            **self._context.model_dump(
                include={
                    "run_id",
                    "event_id",
                    "source_commit",
                    "schema_revision",
                    "manifest_sha256",
                    "scenario_sha256",
                }
            ),
            round_number=progress.round_number,
            round_id=progress.round_id,
            question_id=progress.question_id,
            verification_requested_head_sha256=progress.verification_head,
            generator_evidence=progress.verification.generator_evidence,
        )
        loaded = self._verification_provider.load_receipt(receipt_path, expectation)
        receipt = loaded.receipt
        if (
            receipt.verification_id
            != verification_id(
                self._context.run_id,
                progress.round_number,
                progress.verification_head,
            )
            or receipt.run_id != expectation.run_id
            or receipt.event_id != expectation.event_id
            or receipt.source_commit != expectation.source_commit
            or receipt.schema_revision != expectation.schema_revision
            or receipt.manifest_sha256 != expectation.manifest_sha256
            or receipt.scenario_sha256 != expectation.scenario_sha256
            or receipt.round_number != expectation.round_number
            or receipt.round_id != expectation.round_id
            or receipt.question_id != expectation.question_id
            or receipt.verification_requested_head_sha256
            != expectation.verification_requested_head_sha256
            or receipt.generator_evidence != expectation.generator_evidence
        ):
            raise ControllerRefusal("controller_verification_receipt_mismatch")
        return _VerifiedReceiptInput(
            loaded=loaded,
        )

    def _seal_progress_checkpoint(self, progress: _Progress) -> None:
        if (
            progress.last_plan is None
            or progress.last_success is None
            or progress.verification is None
            or progress.verification_head is None
            or progress.verification_request_artifact is None
            or progress.receipt is None
            or self._journal.head_sha256 is None
        ):
            raise ControllerRefusal("controller_journal_state_invalid")
        checkpoint = _checkpoint_contract(
            self._context,
            round_number=progress.round_number,
            round_id=progress.round_id,
            question_id=progress.question_id,
            journal_head=self._journal.head_sha256,
            plan=progress.last_plan,
            success=progress.last_success,
            verification=progress.verification,
            verification_head=progress.verification_head,
            verification_request_artifact=progress.verification_request_artifact,
            receipt=progress.receipt,
            sealed_at=self._journal.records[-1].recorded_at,
        )
        identity = seal_checkpoint(
            self._checkpoint_directory,
            checkpoint,
            self._repository_root,
        )
        self._journal.append(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            payload=CheckpointSealedPayload(
                kind="checkpoint_sealed",
                round_number=progress.round_number,
                checkpoint=identity,
            ),
        )

    def _validate_checkpoint(
        self,
        expected: ControllerCheckpoint,
        artifact: ArtifactIdentity,
    ) -> None:
        actual = load_checkpoint(
            self._checkpoint_directory,
            artifact,
            self._repository_root,
        )
        if actual != expected:
            raise ControllerRefusal("controller_checkpoint_binding_mismatch")

    def _validate_verification_request(
        self,
        expectation: VerificationRequestExpectation,
        artifact: ArtifactIdentity,
    ) -> None:
        self._verification_provider.validate_verification_request(
            self._verification_request_directory,
            artifact,
            self._manifest,
            expectation,
        )

    def _status(self, progress: _Progress) -> ControllerStatus:
        journal_head = self._journal.head_sha256
        if journal_head is None:
            raise ControllerRefusal("controller_journal_state_invalid")
        if progress.phase == "completed":
            return ControllerStatus(
                run_id=self._context.run_id,
                event_id=self._context.event_id,
                source_commit=self._context.source_commit,
                schema_revision=self._context.schema_revision,
                manifest_sha256=self._context.manifest_sha256,
                scenario_sha256=self._context.scenario_sha256,
                status="completed",
                next_action=None,
                round_number=None,
                round_id=None,
                journal_head_sha256=journal_head,
            )
        request_artifact = progress.verification_request_artifact
        request_head = progress.verification_head if request_artifact is not None else None
        action = LoadTestController._expected_action(progress)
        return ControllerStatus(
            run_id=self._context.run_id,
            event_id=self._context.event_id,
            source_commit=self._context.source_commit,
            schema_revision=self._context.schema_revision,
            manifest_sha256=self._context.manifest_sha256,
            scenario_sha256=self._context.scenario_sha256,
            status="waiting",
            next_action=action,
            round_number=progress.round_number,
            round_id=progress.round_id,
            journal_head_sha256=journal_head,
            verification_requested_head_sha256=request_head,
            verification_request_artifact=request_artifact,
        )
