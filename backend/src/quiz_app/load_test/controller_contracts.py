from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)

from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.verification_contracts import ArtifactIdentity, Sha256
from quiz_app.operator.control_contracts import OperatorMutationResponse

ControllerAction = Literal["prepare", "schedule", "publish", "wait-results", "advance"]
ControllerOperation = Literal[
    "begin_preparation",
    "schedule_round",
    "publish_round_results",
    "advance_round",
    "complete_event",
]
ControllerIdempotencyOperation = ControllerOperation
JSON_VALUE_ADAPTER = TypeAdapter(object)


class ControllerRefusal(ValueError):
    """入力値を含めず、controllerの固定拒否codeだけを表す。"""


class ControllerApiMutationError(RuntimeError):
    """HTTP adapterが秘密値を捨てた後の確定mutation失敗。"""

    def __init__(self, code: Literal["round_backup_failed"]) -> None:
        super().__init__(code)
        self.code = code


class StrictControllerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ControllerContext(StrictControllerModel):
    run_id: UUID
    event_id: UUID
    event_slug: StrictStr
    public_origin: StrictStr
    source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    schema_revision: StrictStr
    scheduled_start_at: AwareDatetime
    manifest_sha256: Sha256
    scenario_sha256: Sha256


class VerificationExpectation(StrictControllerModel):
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    schema_revision: StrictStr
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    verification_requested_head_sha256: Sha256
    generator_evidence: ArtifactIdentity


class VerificationRequestExpectation(VerificationExpectation):
    requested_at: AwareDatetime


class InitializedPayload(StrictControllerModel):
    kind: Literal["initialized"]
    context: ControllerContext
    operator_account_id: UUID
    server_time: AwareDatetime
    event_version: StrictInt = Field(ge=1)
    round_version: StrictInt = Field(ge=1)


class MutationPlannedPayload(StrictControllerModel):
    kind: Literal["mutation_planned"]
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    operation: ControllerOperation
    attempt: StrictInt = Field(ge=1, le=10)
    expected_version: StrictInt = Field(ge=1)
    idempotency_key: UUID
    request_sha256: Sha256


ControllerMutationResponse = OperatorMutationResponse


class MutationSucceededPayload(StrictControllerModel):
    kind: Literal["mutation_succeeded"]
    round_number: StrictInt = Field(ge=1, le=10)
    target_round_id: UUID
    idempotency_key: UUID
    response: ControllerMutationResponse


class MutationFailedPayload(StrictControllerModel):
    kind: Literal["mutation_failed"]
    round_number: StrictInt = Field(ge=1, le=10)
    target_round_id: UUID
    operation: Literal["advance_round", "complete_event"]
    attempt: StrictInt = Field(ge=1, le=10)
    idempotency_key: UUID
    error_code: Literal["round_backup_failed"]


class ResultsObservedPayload(StrictControllerModel):
    kind: Literal["results_observed"]
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    event_version: StrictInt = Field(ge=1)
    round_version: StrictInt = Field(ge=1)
    ranking_snapshot_version: StrictInt = Field(ge=1)
    generator_evidence: ArtifactIdentity


class VerificationRequestedPayload(StrictControllerModel):
    kind: Literal["verification_requested"]
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    generator_evidence: ArtifactIdentity


class VerificationRequestSealedPayload(StrictControllerModel):
    kind: Literal["verification_request_sealed"]
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    verification_requested_head_sha256: Sha256
    request_artifact: ArtifactIdentity


class ReceiptAcceptedPayload(StrictControllerModel):
    kind: Literal["receipt_accepted"]
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    verification_id: UUID
    receipt_artifact: ArtifactIdentity
    report: ArtifactIdentity
    verification_requested_head_sha256: Sha256


class CheckpointSealedPayload(StrictControllerModel):
    kind: Literal["checkpoint_sealed"]
    round_number: StrictInt = Field(ge=1, le=10)
    checkpoint: ArtifactIdentity


JournalPayload = Annotated[
    InitializedPayload
    | MutationPlannedPayload
    | MutationSucceededPayload
    | MutationFailedPayload
    | ResultsObservedPayload
    | VerificationRequestedPayload
    | VerificationRequestSealedPayload
    | ReceiptAcceptedPayload
    | CheckpointSealedPayload,
    Field(discriminator="kind"),
]


class ControllerJournalRecord(StrictControllerModel):
    format: Literal["load-test-controller-journal-v2"]
    sequence: StrictInt = Field(ge=1)
    previous_sha256: Sha256 | None
    recorded_at: AwareDatetime
    run_id: UUID
    event_id: UUID
    payload: JournalPayload
    entry_sha256: Sha256


class ControllerCheckpoint(StrictControllerModel):
    format: Literal["load-test-controller-checkpoint-v2"]
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    schema_revision: StrictStr
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    journal_head_sha256: Sha256
    verification_requested_head_sha256: Sha256
    verification_request_artifact: ArtifactIdentity
    receipt_artifact: ArtifactIdentity
    verification_id: UUID
    report: ArtifactIdentity
    generator_evidence: ArtifactIdentity
    completed_operation: Literal["advance_round", "complete_event"]
    idempotency_key: UUID
    response_sha256: Sha256
    next_round_id: UUID | None
    sealed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_progression(self) -> Self:
        if (self.completed_operation == "advance_round") != (self.next_round_id is not None):
            raise ValueError("advanceだけがnext_round_idを持てます")
        return self


class ControllerStatus(StrictControllerModel):
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    schema_revision: StrictStr
    manifest_sha256: Sha256
    scenario_sha256: Sha256
    status: Literal["waiting", "completed"]
    next_action: ControllerAction | None
    round_number: StrictInt | None
    round_id: UUID | None
    journal_head_sha256: Sha256
    verification_requested_head_sha256: Sha256 | None = None
    verification_request_artifact: ArtifactIdentity | None = None

    @model_validator(mode="after")
    def validate_verification_request_pair(self) -> Self:
        """request の journal head と artifact identity は常に対で公開する。"""

        if (self.verification_requested_head_sha256 is None) != (
            self.verification_request_artifact is None
        ):
            raise ValueError("verification request head and artifact must be paired")
        return self


def canonical_json_bytes(value: BaseModel | dict[str, object]) -> bytes:
    raw = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else JSON_VALUE_ADAPTER.dump_python(value, mode="json")
    )
    return json.dumps(raw, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def manifest_context(manifest: LoadTestManifest) -> ControllerContext:
    return ControllerContext(
        run_id=manifest.run_id,
        event_id=manifest.event.id,
        event_slug=manifest.event.slug,
        public_origin=manifest.public_origin,
        source_commit=manifest.artifacts.source_commit,
        schema_revision=manifest.schema_revision,
        scheduled_start_at=manifest.scheduled_start_at,
        manifest_sha256=sha256(canonical_json_bytes(manifest)).hexdigest(),
        scenario_sha256=sha256(canonical_json_bytes(manifest.scenario)).hexdigest(),
    )


def controller_idempotency_key(
    run_id: UUID,
    round_number: int,
    operation: ControllerIdempotencyOperation,
    attempt: int = 1,
) -> UUID:
    if not 1 <= attempt <= 10:
        raise ControllerRefusal("controller_mutation_attempt_invalid")
    return uuid5(run_id, f"controller:round:{round_number}:{operation}:attempt:{attempt}")


def verification_id(run_id: UUID, round_number: int, journal_head: str) -> UUID:
    return uuid5(run_id, f"verification:round:{round_number}:{journal_head}")


def required_confirmation_phrase(
    context: ControllerContext,
    round_number: int,
    action: ControllerAction,
    receipt_sha256: str | None = None,
) -> str:
    suffix = (
        " EXCLUSIVE-WINDOW 600-VIRTUAL-SESSIONS"
        if action == "prepare" and round_number == 1
        else ""
    )
    if action == "advance":
        if receipt_sha256 is None:
            raise ControllerRefusal("controller_verification_receipt_required")
        suffix = f" DB-VERIFIED {receipt_sha256}"
    return f"CONFIRM {context.run_id} ROUND {round_number:02d} {action.upper()}{suffix}"
