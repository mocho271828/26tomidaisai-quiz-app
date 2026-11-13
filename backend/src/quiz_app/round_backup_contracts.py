"""暗号化backupの保存契約。v1〜v3は読取り専用、新規作成は補助回答を持たないv4。"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from quiz_app.models.enums import (
    RoundResultPublication,
    RoundState,
    ScoreExclusionReason,
    SpeedCohort,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackupRoundRow(_StrictModel):
    id: UUID
    event_id: UUID
    question_id: UUID
    version: int = Field(ge=1)
    state: RoundState | Literal["StandardClosedAssistedOpen"]
    result_publication: RoundResultPublication
    open_at: datetime | None
    standard_close_at: datetime | None
    assisted_close_at: datetime | None
    revealed_at: datetime | None
    void_reason: str | None
    created_at: datetime | None


class BackupQuestionRow(_StrictModel):
    id: UUID
    event_id: UUID
    version: int = Field(ge=1)
    prompt: str
    explanation: str
    image_url: str | None
    image_alt: str | None
    display_order: int
    created_at: datetime | None


class BackupQuestionOptionRow(_StrictModel):
    id: UUID
    question_id: UUID
    label: str
    content: str
    image_url: str | None
    image_alt: str | None
    display_order: int
    is_correct: bool


class BackupAnswerRow(_StrictModel):
    id: UUID
    participant_id: UUID
    round_id: UUID
    option_id: UUID
    client_answer_id: UUID
    received_at: datetime | None


class BackupScoreLedgerRow(_StrictModel):
    id: UUID
    round_id: UUID
    participant_id: UUID
    scoring_policy_version: str
    is_correct: bool
    base_points: int
    speed_points: int
    total_points: int
    elapsed_ms: int | None
    speed_bucket: int | None
    speed_rank: int | None
    speed_cohort_size: int | None
    speed_cohort: SpeedCohort
    exclusion_reason: ScoreExclusionReason | Literal["Assisted"]


class BackupReadinessReportRow(_StrictModel):
    id: UUID
    participant_id: UUID
    round_id: UUID
    round_version: int = Field(ge=1)
    is_ready: bool
    has_asset_failure: bool
    reported_at: datetime | None
    last_seen_at: datetime | None


class BackupPreparationTargetRow(_StrictModel):
    id: UUID
    round_id: UUID
    participant_id: UUID
    captured_at: datetime | None


class BackupOptionStatRow(_StrictModel):
    id: UUID
    round_id: UUID
    option_id: UUID
    valid_response_count: int = Field(ge=0)
    response_percentage: Decimal = Field(ge=0, le=100)


class BackupRankSnapshotRow(_StrictModel):
    id: UUID
    event_id: UUID
    round_id: UUID
    version: int = Field(ge=1)
    generated_at: datetime | None


class BackupRankingRow(_StrictModel):
    id: UUID
    snapshot_id: UUID
    public_label: str
    overall_rank: int = Field(ge=1)
    total_points: int = Field(ge=0)
    correct_count: int = Field(ge=0)


class _RoundBackupPayload(_StrictModel):
    round: BackupRoundRow
    question: BackupQuestionRow
    question_options: list[BackupQuestionOptionRow]
    score_ledgers: list[BackupScoreLedgerRow]
    readiness_reports: list[BackupReadinessReportRow]
    preparation_targets: list[BackupPreparationTargetRow]
    option_stats: list[BackupOptionStatRow]
    rank_snapshots: list[BackupRankSnapshotRow]
    rank_snapshot_rows: list[BackupRankingRow]


class RoundBackupPayload(_RoundBackupPayload):
    answers: list[BackupAnswerRow]


class ClientBackupAnswerRow(BackupAnswerRow):
    client_elapsed_ms: StrictInt = Field(ge=0)
    is_assisted: bool


class _BackupScoringPolicy(_StrictModel):
    version: str = Field(min_length=1, max_length=64)
    timing_basis: Literal["client_monotonic_v1"]
    arrival_grace_ms: Literal[2000]
    base_points: StrictInt = Field(ge=0)
    initial_speed_bonus: StrictInt = Field(ge=0)
    speed_bucket_ms: StrictInt = Field(gt=0)
    speed_decrement_per_bucket: StrictInt = Field(ge=0)
    standard_duration_ms: Literal[10000]
    is_approved: Literal[True]


class BackupScoringPolicy(_BackupScoringPolicy):
    clock_tolerance_ms: Literal[250]


class ClientAheadBackupScoringPolicy(_BackupScoringPolicy):
    clock_tolerance_ms: Literal[500]
    client_ahead_tolerance_ms: Literal[2500]


class _ClientRoundBackupPayload(_RoundBackupPayload):
    answers: list[ClientBackupAnswerRow]

    def _validate_official_timing(
        self,
        policy: BackupScoringPolicy | ClientAheadBackupScoringPolicy,
        client_ahead_tolerance_ms: int,
    ) -> Self:
        """再採点せず、保存した正式時間と受付条件・policyの対応を検証する。"""

        answers_by_participant = {answer.participant_id: answer for answer in self.answers}
        if len(answers_by_participant) != len(self.answers):
            raise ValueError("round backup has duplicate answer participants")
        for answer in self.answers:
            round_ = self.round
            close_at = round_.assisted_close_at if answer.is_assisted else round_.standard_close_at
            if round_.open_at is None or close_at is None or answer.received_at is None:
                raise ValueError("round backup answer timing is incomplete")
            duration_ms = 15000 if answer.is_assisted else policy.standard_duration_ms
            if close_at != round_.open_at + timedelta(milliseconds=duration_ms):
                raise ValueError("round backup answer deadline does not match policy")
            if (
                not round_.open_at
                <= answer.received_at
                < close_at + timedelta(milliseconds=policy.arrival_grace_ms)
            ):
                raise ValueError("round backup answer arrived outside acceptance window")
            if answer.client_elapsed_ms >= duration_ms:
                raise ValueError("round backup client elapsed time exceeds selection deadline")
            received_ms = (answer.received_at - round_.open_at) // timedelta(milliseconds=1)
            difference_ms = received_ms - answer.client_elapsed_ms
            if (
                not -client_ahead_tolerance_ms
                <= difference_ms
                <= (policy.arrival_grace_ms + policy.clock_tolerance_ms)
            ):
                raise ValueError("round backup answer timing difference is invalid")
        for ledger in self.score_ledgers:
            answer = answers_by_participant.get(ledger.participant_id)
            if (
                answer is None
                or ledger.elapsed_ms != answer.client_elapsed_ms
                or ledger.scoring_policy_version != policy.version
            ):
                raise ValueError("round backup official timing does not match score ledger")
        return self


class ClientRoundBackupPayload(_ClientRoundBackupPayload):
    scoring_policy: BackupScoringPolicy

    @model_validator(mode="after")
    def validate_official_timing(self) -> Self:
        # v2の下限は新しいpolicy設定に追従させず、保存当時の250msへ固定する。
        return self._validate_official_timing(
            self.scoring_policy, self.scoring_policy.clock_tolerance_ms
        )


class ClientAheadRoundBackupPayload(_ClientRoundBackupPayload):
    scoring_policy: ClientAheadBackupScoringPolicy

    @model_validator(mode="after")
    def validate_official_timing(self) -> Self:
        return self._validate_official_timing(
            self.scoring_policy, self.scoring_policy.client_ahead_tolerance_ms
        )


class UnifiedBackupRoundRow(_StrictModel):
    id: UUID
    event_id: UUID
    question_id: UUID
    version: int = Field(ge=1)
    state: RoundState
    result_publication: RoundResultPublication
    open_at: datetime | None
    standard_close_at: datetime | None
    revealed_at: datetime | None
    void_reason: str | None
    created_at: datetime | None


class UnifiedBackupAnswerRow(BackupAnswerRow):
    client_elapsed_ms: StrictInt | None = Field(ge=0)


class UnifiedBackupScoringPolicy(_StrictModel):
    version: str = Field(min_length=1, max_length=64)
    timing_basis: Literal["server_received_v1", "client_monotonic_v1"]
    arrival_grace_ms: StrictInt = Field(ge=0)
    clock_tolerance_ms: StrictInt = Field(ge=0)
    client_ahead_tolerance_ms: StrictInt = Field(ge=0)
    base_points: StrictInt = Field(ge=0)
    initial_speed_bonus: StrictInt = Field(ge=0)
    speed_bucket_ms: StrictInt = Field(gt=0)
    speed_decrement_per_bucket: StrictInt = Field(ge=0)
    standard_duration_ms: Literal[10000]
    is_approved: Literal[True]


class UnifiedRoundBackupPayload(_StrictModel):
    round: UnifiedBackupRoundRow
    question: BackupQuestionRow
    question_options: list[BackupQuestionOptionRow]
    answers: list[UnifiedBackupAnswerRow]
    score_ledgers: list[BackupScoreLedgerRow]
    readiness_reports: list[BackupReadinessReportRow]
    preparation_targets: list[BackupPreparationTargetRow]
    option_stats: list[BackupOptionStatRow]
    rank_snapshots: list[BackupRankSnapshotRow]
    rank_snapshot_rows: list[BackupRankingRow]
    scoring_policy: UnifiedBackupScoringPolicy

    @model_validator(mode="after")
    def validate_saved_timing(self) -> Self:
        policy, round_ = self.scoring_policy, self.round
        answers = {answer.participant_id: answer for answer in self.answers}
        if len(answers) != len(self.answers):
            raise ValueError("round backup has duplicate answer participants")
        official_elapsed_by_participant: dict[UUID, int] = {}
        for answer in self.answers:
            if (
                round_.open_at is None
                or round_.standard_close_at is None
                or answer.received_at is None
            ):
                raise ValueError("round backup answer timing is incomplete")
            if round_.standard_close_at != round_.open_at + timedelta(
                milliseconds=policy.standard_duration_ms
            ):
                raise ValueError("round backup answer deadline does not match policy")
            if (
                not round_.open_at
                <= answer.received_at
                < round_.standard_close_at + timedelta(milliseconds=policy.arrival_grace_ms)
            ):
                raise ValueError("round backup answer arrived outside acceptance window")
            received_ms = (answer.received_at - round_.open_at) // timedelta(milliseconds=1)
            if policy.timing_basis == "server_received_v1":
                if answer.client_elapsed_ms is not None:
                    raise ValueError("round backup legacy answer has client time")
                official_elapsed_by_participant[answer.participant_id] = received_ms
                continue
            if (
                answer.client_elapsed_ms is None
                or answer.client_elapsed_ms >= policy.standard_duration_ms
            ):
                raise ValueError("round backup client elapsed time exceeds selection deadline")
            difference_ms = received_ms - answer.client_elapsed_ms
            if (
                not -policy.client_ahead_tolerance_ms
                <= difference_ms
                <= policy.arrival_grace_ms + policy.clock_tolerance_ms
            ):
                raise ValueError("round backup answer timing difference is invalid")
            official_elapsed_by_participant[answer.participant_id] = answer.client_elapsed_ms
        for ledger in self.score_ledgers:
            answer = answers.get(ledger.participant_id)
            if ledger.exclusion_reason == "Assisted":
                raise ValueError("round backup retired answer category")
            if answer is None or ledger.scoring_policy_version != policy.version:
                raise ValueError("round backup score ledger does not match answer policy")
            if ledger.elapsed_ms != official_elapsed_by_participant[answer.participant_id]:
                raise ValueError("round backup official timing does not match score ledger")
        return self


AnyRoundBackupPayload: TypeAlias = (
    RoundBackupPayload
    | ClientRoundBackupPayload
    | ClientAheadRoundBackupPayload
    | UnifiedRoundBackupPayload
)


class RoundBackupCounts(_StrictModel):
    rounds: int = Field(ge=0)
    questions: int = Field(ge=0)
    question_options: int = Field(ge=0)
    answers: int = Field(ge=0)
    score_ledgers: int = Field(ge=0)
    readiness_reports: int = Field(ge=0)
    preparation_targets: int = Field(ge=0)
    option_stats: int = Field(ge=0)
    rank_snapshots: int = Field(ge=0)
    rank_snapshot_rows: int = Field(ge=0)


class RoundBackupArtifact(_StrictModel):
    format: Literal["quiz-round-backup-v1"]
    idempotency_key: UUID
    artifact_suffix: str = Field(min_length=1, max_length=24)
    plaintext_sha256: str = Field(pattern=SHA256_PATTERN)
    counts: RoundBackupCounts
    payload: RoundBackupPayload

    @model_validator(mode="after")
    def validate_embedded_integrity(self) -> Self:
        if self.counts != counts_for_payload(self.payload):
            raise ValueError("round backup row counts do not match payload")
        payload_digest = plaintext_sha256(self.payload)
        if not hmac.compare_digest(self.plaintext_sha256, payload_digest):
            raise ValueError("round backup plaintext checksum does not match payload")
        if self.payload.round.question_id != self.payload.question.id:
            raise ValueError("round backup question does not match round")
        return self


class ClientRoundBackupArtifact(_StrictModel):
    format: Literal["quiz-round-backup-v2"]
    idempotency_key: UUID
    artifact_suffix: str = Field(min_length=1, max_length=24)
    plaintext_sha256: str = Field(pattern=SHA256_PATTERN)
    counts: RoundBackupCounts
    payload: ClientRoundBackupPayload

    @model_validator(mode="after")
    def validate_embedded_integrity(self) -> Self:
        if self.counts != counts_for_payload(self.payload):
            raise ValueError("round backup row counts do not match payload")
        if not hmac.compare_digest(self.plaintext_sha256, plaintext_sha256(self.payload)):
            raise ValueError("round backup plaintext checksum does not match payload")
        if self.payload.round.question_id != self.payload.question.id:
            raise ValueError("round backup question does not match round")
        return self


class ClientAheadRoundBackupArtifact(_StrictModel):
    format: Literal["quiz-round-backup-v3"]
    idempotency_key: UUID
    artifact_suffix: str = Field(min_length=1, max_length=24)
    plaintext_sha256: str = Field(pattern=SHA256_PATTERN)
    counts: RoundBackupCounts
    payload: ClientAheadRoundBackupPayload

    @model_validator(mode="after")
    def validate_embedded_integrity(self) -> Self:
        if self.counts != counts_for_payload(self.payload):
            raise ValueError("round backup row counts do not match payload")
        if not hmac.compare_digest(self.plaintext_sha256, plaintext_sha256(self.payload)):
            raise ValueError("round backup plaintext checksum does not match payload")
        if self.payload.round.question_id != self.payload.question.id:
            raise ValueError("round backup question does not match round")
        return self


class UnifiedRoundBackupArtifact(_StrictModel):
    format: Literal["quiz-round-backup-v4"]
    idempotency_key: UUID
    artifact_suffix: str = Field(min_length=1, max_length=24)
    plaintext_sha256: str = Field(pattern=SHA256_PATTERN)
    counts: RoundBackupCounts
    payload: UnifiedRoundBackupPayload

    @model_validator(mode="after")
    def validate_embedded_integrity(self) -> Self:
        if self.counts != counts_for_payload(self.payload):
            raise ValueError("round backup row counts do not match payload")
        if not hmac.compare_digest(self.plaintext_sha256, plaintext_sha256(self.payload)):
            raise ValueError("round backup plaintext checksum does not match payload")
        if self.payload.round.question_id != self.payload.question.id:
            raise ValueError("round backup question does not match round")
        return self


AnyRoundBackupArtifact: TypeAlias = (
    RoundBackupArtifact
    | ClientRoundBackupArtifact
    | ClientAheadRoundBackupArtifact
    | UnifiedRoundBackupArtifact
)
ROUND_BACKUP_ADAPTER: TypeAdapter[AnyRoundBackupArtifact] = TypeAdapter(
    Annotated[AnyRoundBackupArtifact, Field(discriminator="format")]
)


class RoundBackupReceipt(_StrictModel):
    artifact_name: str
    plaintext_sha256: str = Field(pattern=SHA256_PATTERN)
    counts: RoundBackupCounts
    round_id: UUID
    round_version: int = Field(ge=1)
    is_replay: bool


def canonical_json(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def plaintext_sha256(payload: AnyRoundBackupPayload) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def counts_for_payload(payload: AnyRoundBackupPayload) -> RoundBackupCounts:
    return RoundBackupCounts(
        rounds=1,
        questions=1,
        question_options=len(payload.question_options),
        answers=len(payload.answers),
        score_ledgers=len(payload.score_ledgers),
        readiness_reports=len(payload.readiness_reports),
        preparation_targets=len(payload.preparation_targets),
        option_stats=len(payload.option_stats),
        rank_snapshots=len(payload.rank_snapshots),
        rank_snapshot_rows=len(payload.rank_snapshot_rows),
    )
