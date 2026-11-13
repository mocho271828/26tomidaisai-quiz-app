from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

from quiz_app.load_test.db_verifier_contracts import (
    DbVerifierRequest,
    QueryExecutionMetadata,
)
from quiz_app.load_test.db_verifier_oracle import OracleAnswer
from quiz_app.load_test.verification_contracts import ArtifactIdentity

FailureCode = StrictStr


class DbVerifierError(ValueError):
    """秘密値やDB行を含めない固定codeで検証を拒否する。"""


class _StrictReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RoundVerificationFacts(_StrictReportModel):
    preparation_target_count: StrictInt = Field(ge=0)
    readiness_report_count: StrictInt = Field(ge=0)
    ready_count: StrictInt = Field(ge=0)
    asset_failure_count: StrictInt = Field(ge=0)
    answer_count: StrictInt = Field(ge=0)
    virtual_answer_count: StrictInt = Field(ge=0)
    score_ledger_count: StrictInt = Field(ge=0)
    option_stat_count: StrictInt = Field(ge=0)
    ranking_row_count: StrictInt = Field(ge=0)
    operation_audit_count: StrictInt = Field(ge=0)
    idempotency_record_count: StrictInt = Field(ge=0)


class FinalVerificationFacts(_StrictReportModel):
    participant_count: StrictInt = Field(ge=0)
    preparation_target_count: StrictInt = Field(ge=0)
    readiness_report_count: StrictInt = Field(ge=0)
    answer_count: StrictInt = Field(ge=0)
    score_ledger_count: StrictInt = Field(ge=0)
    virtual_answer_count: StrictInt = Field(ge=0)
    option_stat_count: StrictInt = Field(ge=0)
    rank_snapshot_count: StrictInt = Field(ge=0)
    rank_snapshot_row_count: StrictInt = Field(ge=0)
    experienced_participant_count: StrictInt = Field(ge=0)
    nonresponder_count: StrictInt = Field(ge=0)
    idempotency_record_count: StrictInt = Field(ge=0)
    preparation_audit_count: StrictInt = Field(ge=0)
    schedule_audit_count: StrictInt = Field(ge=0)
    aggregation_audit_count: StrictInt = Field(ge=0)
    publish_audit_count: StrictInt = Field(ge=0)
    advance_audit_count: StrictInt = Field(ge=0)
    complete_audit_count: StrictInt = Field(ge=0)
    backup_validation_audit_count: StrictInt = Field(ge=0)


class _BoundVerificationReport(_StrictReportModel):
    verification_id: UUID
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    schema_revision: StrictStr = Field(min_length=1, max_length=128)
    manifest_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    round_number: StrictInt = Field(ge=1, le=10)
    round_id: UUID
    question_id: UUID
    verification_requested_head_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    generator_evidence: ArtifactIdentity
    input_artifact: ArtifactIdentity
    query_executions: tuple[QueryExecutionMetadata, ...]
    decision: Literal["PASS", "FAIL"]
    failure_codes: tuple[FailureCode, ...]
    sealed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if (self.decision == "PASS") != (not self.failure_codes):
            raise ValueError("decision and failure codes disagree")
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError("failure codes must be unique")
        return self


class PerRoundVerificationReport(_BoundVerificationReport):
    format: Literal["load-test-db-round-verification-report-v2"]
    mode: Literal["per-round"]
    facts: RoundVerificationFacts


class FinalVerificationReport(_BoundVerificationReport):
    format: Literal["load-test-db-final-verification-report-v2"]
    mode: Literal["final"]
    round_identity_scope: Literal[
        "round-1-and-round-10-exact;round-2-through-9-unique-and-question-bound"
    ]
    facts: FinalVerificationFacts


class LockObservationFacts(_StrictReportModel):
    sample_interval_seconds: Literal[1]
    sample_count: StrictInt = Field(ge=1)
    first_observed_at: AwareDatetime
    last_observed_at: AwareDatetime
    peak_total_lock_count: StrictInt = Field(ge=0)
    peak_waiting_lock_count: StrictInt = Field(ge=0)
    peak_waiting_session_count: StrictInt = Field(ge=0)
    peak_active_transaction_count: StrictInt = Field(ge=0)
    peak_idle_in_transaction_count: StrictInt = Field(ge=0)
    peak_oldest_transaction_age_ms: Decimal = Field(ge=Decimal("0"))
    final_waiting_lock_count: StrictInt = Field(ge=0)
    final_waiting_session_count: StrictInt = Field(ge=0)
    peak_interpretation: Literal["calibration-only"]


class LockObservationReport(_StrictReportModel):
    format: Literal["load-test-db-lock-observation-report-v1"]
    mode: Literal["observe-locks"]
    run_id: UUID
    event_id: UUID
    source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    schema_revision: StrictStr = Field(min_length=1, max_length=128)
    manifest_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    round_number: StrictInt = Field(ge=1, le=10)
    input_artifact: ArtifactIdentity
    query_executions: tuple[QueryExecutionMetadata, ...]
    facts: LockObservationFacts
    decision: Literal["PASS", "FAIL"]
    failure_codes: tuple[FailureCode, ...]
    sealed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if (self.decision == "PASS") != (not self.failure_codes):
            raise ValueError("decision and failure codes disagree")
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError("failure codes must be unique")
        return self


DbVerifierReport = PerRoundVerificationReport | FinalVerificationReport | LockObservationReport


class DbVerifierRunResult(_StrictReportModel):
    mode: Literal["per-round", "final", "observe-locks"]
    decision: Literal["PASS", "FAIL"]
    report: ArtifactIdentity
    receipt: ArtifactIdentity | None

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if (self.receipt is not None) != (
            self.decision == "PASS" and self.mode in {"per-round", "final"}
        ):
            raise ValueError("only passing verification has a receipt")
        return self


@dataclass(frozen=True, slots=True)
class LoadedDbVerifierRequest:
    value: DbVerifierRequest
    artifact: ArtifactIdentity


@dataclass(frozen=True, slots=True)
class AnswerCheck:
    is_valid: bool
    virtual_count: int
    score_inputs: tuple[OracleAnswer, ...]
    virtual_participants: Mapping[int, UUID]


Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
