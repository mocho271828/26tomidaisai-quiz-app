"""起動前の予定母数と6問の個別集合照合に使う匿名の境界契約。"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RunId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
WorkerId = Annotated[str, Field(pattern=r"^worker-[1-9][0-9]*$")]
Commit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Serial = Annotated[int, Field(strict=True, ge=1, le=17576)]
RoundNumber = Annotated[int, Field(strict=True, ge=1, le=6)]
Count = Annotated[int, Field(strict=True, ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SnapshotMetadata(StrictModel):
    transaction_read_only: Literal[True]
    isolation_level: Literal["repeatable read"]
    event_count: Literal[1]
    round_count: Annotated[int, Field(strict=True, ge=0, le=6)]
    void_history: Literal[0]
    unmatched_answers: Literal[0]
    participant_count: Count
    answer_count: Count
    captured_at: AwareDatetime


class StoredAnswer(StrictModel):
    participant_key: Digest
    round_number: RoundNumber


class MembershipSnapshot(StrictModel):
    format: Literal["browser-membership-snapshot-v1"] = "browser-membership-snapshot-v1"
    run_id: RunId
    source_commit: Commit
    identity_key_fingerprint: Digest
    metadata: SnapshotMetadata
    participants: list[Digest] = Field(max_length=17576)
    answers: list[StoredAnswer] = Field(max_length=105456)


class BrowserConditions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_id: RunId = Field(alias="runId")
    source_commit: Commit = Field(alias="sourceCommit")
    count: Annotated[int, Field(strict=True, ge=1, le=100)]
    ordinal_start: Serial = Field(alias="ordinalStart")
    worker_id: WorkerId | None = Field(default=None, alias="workerId")


class Cohort(StrictModel):
    count: Annotated[int, Field(strict=True, ge=1, le=100)]
    ordinal_start: Serial = Field(alias="ordinalStart")


class PlannedWorker(Cohort):
    worker_id: WorkerId = Field(alias="workerId")


class BrowserManifest(StrictModel):
    format: Literal["browser-control-manifest-v1"]
    run_id: RunId = Field(alias="runId")
    source_commit: Commit = Field(alias="sourceCommit")
    total_vus: Annotated[int, Field(strict=True, ge=1, le=650)] = Field(alias="totalVus")
    expected_rounds: Literal[6] = Field(alias="expectedRounds")
    manual_participant_count: Annotated[int, Field(strict=True, ge=0, le=100)] = Field(
        alias="manualParticipantCount"
    )
    workers: list[PlannedWorker] = Field(min_length=1, max_length=650)

    @model_validator(mode="after")
    def validate_workers(self) -> Self:
        """受信済みworkerだけで予定母数を縮小できない契約にする。"""
        ordered = sorted(self.workers, key=lambda worker: worker.ordinal_start)
        if len({worker.worker_id for worker in ordered}) != len(ordered):
            raise ValueError("membership_manifest_duplicate_worker")
        if sum(worker.count for worker in ordered) != self.total_vus:
            raise ValueError("membership_manifest_worker_sum")
        if ordered[-1].ordinal_start + ordered[-1].count - 1 > 17576:
            raise ValueError("membership_manifest_serial_limit")
        if any(
            previous.ordinal_start + previous.count != current.ordinal_start
            for previous, current in zip(ordered, ordered[1:], strict=False)
        ):
            raise ValueError("membership_manifest_worker_ranges")
        if self.total_vus + self.manual_participant_count > 17576:
            raise ValueError("membership_manifest_participant_limit")
        return self


class ManualParticipantAllowlist(StrictModel):
    format: Literal["browser-manual-allowlist-v1"] = "browser-manual-allowlist-v1"
    run_id: RunId
    identity_key_fingerprint: Digest
    participant_keys: list[Digest] = Field(max_length=17576)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> Self:
        if len(set(self.participant_keys)) != len(self.participant_keys):
            raise ValueError("membership_manual_allowlist_duplicate")
        return self


class AnswerObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    phase: str | None = None
    round_number: Annotated[int, Field(strict=True, ge=0, le=6)] | None = None


class BrowserEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    event: str
    reason: str | None = None
    sequence: Count | None = None
    monotonic_ms: float = Field(alias="monotonicMs", ge=0)
    lifecycle_version: Literal[2] | None = Field(default=None, alias="lifecycleVersion")
    serial: Serial | None = None
    round: Annotated[int, Field(strict=True, ge=0, le=6)] | None = None
    cohort: Cohort | None = None
    participant_key: Digest | None = Field(default=None, alias="participantKey")
    identity_key_fingerprint: Digest | None = Field(default=None, alias="identityKeyFingerprint")
    telemetry: AnswerObservation | None = None


class RecordingHealth(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    complete: bool = Field(strict=True)
    attempted_records: Count = Field(alias="attemptedRecords")
    accepted_records: Count = Field(alias="acceptedRecords")
    written_records: Count = Field(alias="writtenRecords")
    dropped_records: Count = Field(alias="droppedRecords")
    failed_records: Count = Field(alias="failedRecords")
    pending_records: Count = Field(alias="pendingRecords")

    def is_complete(self) -> bool:
        return (
            self.state == "closed"
            and self.complete
            and self.dropped_records == self.failed_records == self.pending_records == 0
            and self.attempted_records == self.accepted_records == self.written_records
        )


class BrowserRun(StrictModel):
    conditions: BrowserConditions
    events: list[BrowserEvent]
    recording_health: RecordingHealth | None = None


class Pair(StrictModel):
    serial: Serial
    round: RoundNumber


class DuplicatePair(Pair):
    occurrences: Annotated[int, Field(strict=True, ge=2)]


class PairComparison(StrictModel):
    observed_rows: Count
    distinct_pairs: Count
    missing: list[Pair]
    extra: list[Pair]
    duplicates: list[DuplicatePair]


class MembershipReport(StrictModel):
    format: Literal["browser-membership-report-v2"] = "browser-membership-report-v2"
    run_id: RunId
    source_commit: Commit
    identity_key_fingerprint: Digest
    decision: Literal["PASS", "FAIL"]
    expected_participants: Annotated[int, Field(strict=True, ge=1, le=650)] = 100
    expected_rounds: Literal[6] = 6
    expected_pairs: Annotated[int, Field(strict=True, ge=6, le=3900)] = 600
    expected_manual_participants: Count = 0
    allowlisted_manual_participants: Count = 0
    observed_manual_participants: Count = 0
    observed_manual_answers: Count = 0
    missing_manual_participants: Count = 0
    manual_identity_conflicts: Count = 0
    missing_workers: list[RunId] = Field(default_factory=list)
    incomplete_workers: list[RunId] = Field(default_factory=list)
    recording_incomplete_workers: list[RunId] = Field(default_factory=list)
    observed_database_rounds: Annotated[int, Field(strict=True, ge=0, le=6)]
    mapped_participants: Count
    unmapped_browser_participants: list[Serial]
    unmapped_database_participants: Count
    unmapped_database_answers: Count
    duplicate_database_participants: Count
    duplicate_identity_observations: Count
    shared_identity_keys: Count
    outside_cohort_identity_observations: Count
    outside_cohort_observations: Count
    rejected_identity_observations: Count
    excluded_outside_measurement_events: Count
    comparisons: dict[Literal["stored", "accepted", "results", "clicks", "sending"], PairComparison]
    stored_without_accepted: list[Pair]
    accepted_without_stored: list[Pair]
    stored_without_results: list[Pair]
    results_without_stored: list[Pair]
    all_expected_membership_verified: bool
    input_sha256: dict[str, Digest] = Field(default_factory=dict)
    scope: str = (
        "人×問の保存/受付DOM/本人結果DOM/試行の照合。DB未生成の問も予定人数×6問の不足に含め、"
        "途中中止を全6問実施として扱わない。DBはsnapshot取得時の保存存在で、"
        "測定窓内のcommit完了時刻を保証しない。点数・順位・可視paint・600VU耐性は別検証。"
    )
