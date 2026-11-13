from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

from pydantic import BaseModel

from quiz_app.load_test.controller_contracts import (
    canonical_json_bytes,
    controller_idempotency_key,
    manifest_context,
    verification_id,
)
from quiz_app.load_test.controller_verification import (
    GeneratorRoundEvidence,
    RoundOptionCounts,
    RoundVerificationOracle,
    RoundVerificationRequest,
)
from quiz_app.load_test.db_verifier import LoadedDbVerifierRequest
from quiz_app.load_test.db_verifier_contracts import StrictDbVerifierModel
from quiz_app.load_test.db_verifier_oracle import (
    OracleAnswer,
    OracleHistoricalLedger,
    OracleParticipant,
    OracleScoringPolicy,
    expected_answering_vu_ids,
    expected_virtual_client_answer_id,
    expected_virtual_option,
    rank_overall_independently,
    score_round_independently,
)
from quiz_app.load_test.db_verifier_queries import (
    AnswerRow,
    DbQuerySpec,
    FinalEventRow,
    FinalIntegritySummaryRow,
    FinalParticipantSummaryRow,
    FinalRoundRow,
    IdempotencyRecordRow,
    OperationAuditRow,
    OptionStatRow,
    RankingConsistencyRow,
    ReadinessSummaryRow,
    RoundIdentityRow,
    ScoreLedgerRow,
    ScoringPolicyRow,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest
from quiz_app.load_test.verification_contracts import (
    ArtifactIdentity,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_AT = datetime(2026, 9, 3, tzinfo=UTC)
SOURCE_COMMIT = "a" * 40
JOURNAL_HEAD = "b" * 64
IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
OPERATOR_ID = UUID(int=80_000)


def _manifest() -> LoadTestManifest:
    return build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-03",
        scheduled_start_at=CREATED_AT + timedelta(hours=1),
        public_origin="https://quiz.aws.internal",
        source_commit=SOURCE_COMMIT,
        api_image=IMAGE,
        gateway_image=IMAGE,
        generator_image=IMAGE,
        scoring_policy_version="scoring-v1",
        scoring_base_points=10_000,
        scoring_initial_speed_bonus=500,
        scoring_speed_bucket_ms=100,
        scoring_speed_decrement_per_bucket=10,
        scoring_standard_duration_ms=10_000,
        scoring_is_approved=True,
        display_name_policy_version="display-v1",
        display_notice_version="notice-v1",
        display_max_code_points=20,
        display_reject_patterns_json="[]",
        created_at=CREATED_AT,
    )


def _round_id(manifest: LoadTestManifest, number: int) -> UUID:
    return manifest.initial_round.id if number == 1 else UUID(int=number, version=4)


def _open_at(manifest: LoadTestManifest, number: int) -> datetime:
    scenario = manifest.scenario.rounds[number - 1]
    return manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)


def _artifact(name: str, digit: str) -> ArtifactIdentity:
    return ArtifactIdentity(name=name, sha256=digit * 64, size_bytes=100)


@dataclass(frozen=True, slots=True)
class _EvidenceFiles:
    round_id: UUID
    generator_path: Path
    generator_artifact: ArtifactIdentity


def _request(
    manifest: LoadTestManifest,
    number: int,
    evidence: _EvidenceFiles | None = None,
) -> RoundVerificationRequest:
    context = manifest_context(manifest)
    scenario = manifest.scenario.rounds[number - 1]
    status_count = 50 if scenario.condition == "receipt-discard" else 0
    replay_count = scenario.virtual_answer_count if scenario.replay == "all" else 0
    return RoundVerificationRequest(
        format="load-test-round-verification-request-v2",
        verification_id=verification_id(RUN_ID, number, JOURNAL_HEAD),
        run_id=RUN_ID,
        event_id=EVENT_ID,
        source_commit=SOURCE_COMMIT,
        schema_revision=manifest.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=number,
        round_id=(evidence.round_id if evidence is not None else _round_id(manifest, number)),
        question_id=manifest.questions[number - 1].id,
        scenario_condition=scenario.condition,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator_evidence=(
            evidence.generator_artifact
            if evidence is not None
            else _artifact(f"generator-{number}.json", "1")
        ),
        oracle=RoundVerificationOracle(
            total_answer_count=500,
            virtual_session_count=600,
            virtual_answer_count=scenario.virtual_answer_count,
            option_counts=RoundOptionCounts(a=126, b=125, c=125, d=124),
            round_start_width_max_ms=1_000,
            unresolved_answer_maximum=0,
            missed_round_maximum=0,
            duplicate_answer_maximum=0,
            misleading_success_maximum=0,
            replay_attempt_count=replay_count,
            replay_same_answer_id_count=replay_count,
            replay_row_change_maximum=0,
            receipt_discard_count=status_count,
            answer_status_query_count=status_count,
            status_recovered_count=status_count,
        ),
        requested_at=_open_at(manifest, number) + timedelta(seconds=20),
    )


def _write_evidence_file(path: Path, value: BaseModel) -> ArtifactIdentity:
    body = canonical_json_bytes(value) + b"\n"
    path.write_bytes(body)
    path.chmod(0o600)
    return ArtifactIdentity(
        name=path.name,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def _evidence_files(
    tmp_path: Path,
    manifest: LoadTestManifest,
    number: int,
    *,
    round_id_override: UUID | None = None,
) -> _EvidenceFiles:
    context = manifest_context(manifest)
    scenario = manifest.scenario.rounds[number - 1]
    question = manifest.questions[number - 1]
    round_id = round_id_override or _round_id(manifest, number)
    open_at = _open_at(manifest, number)
    virtual_counts = Counter(
        expected_virtual_option(vu_id)
        for vu_id in expected_answering_vu_ids(scenario.answer_population)
    )
    replay_count = scenario.virtual_answer_count if scenario.replay == "all" else 0
    status_count = 50 if scenario.condition == "receipt-discard" else 0
    generator = GeneratorRoundEvidence(
        format="load-test-generator-round-evidence-v2",
        run_id=RUN_ID,
        event_id=EVENT_ID,
        source_commit=SOURCE_COMMIT,
        schema_revision=manifest.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=number,
        round_id=round_id,
        question_id=question.id,
        scenario_condition=scenario.condition,
        answer_population=scenario.answer_population,
        virtual_session_count=600,
        virtual_answer_count=scenario.virtual_answer_count,
        virtual_option_counts=RoundOptionCounts(
            a=virtual_counts["A"],
            b=virtual_counts["B"],
            c=virtual_counts["C"],
            d=virtual_counts["D"],
        ),
        started_answer_count=scenario.virtual_answer_count,
        resolved_answer_count=scenario.virtual_answer_count,
        request_start_width_ms=100,
        unresolved_answer_count=0,
        missed_round_count=0,
        duplicate_answer_count=0,
        misleading_success_count=0,
        replay_attempt_count=replay_count,
        replay_same_answer_id_count=replay_count,
        replay_row_change_count=0,
        receipt_discard_count=status_count,
        answer_status_query_count=status_count,
        status_recovered_count=status_count,
        sealed_at=open_at + timedelta(seconds=12),
    )
    directory = _directory(tmp_path, f"evidence-{number}")
    generator_path = directory / f"generator-{number}.json"
    return _EvidenceFiles(
        round_id=round_id,
        generator_path=generator_path,
        generator_artifact=_write_evidence_file(generator_path, generator),
    )


def _policy(manifest: LoadTestManifest) -> ScoringPolicyRow:
    value = manifest.event.scoring_policy
    return ScoringPolicyRow(
        scoring_policy_version=value.version,
        base_points=value.base_points,
        initial_speed_bonus=value.initial_speed_bonus,
        speed_bucket_ms=value.speed_bucket_ms,
        speed_decrement_per_bucket=value.speed_decrement_per_bucket,
        standard_duration_ms=value.standard_duration_ms,
        is_approved=True,
    )


def _answers(manifest: LoadTestManifest, number: int) -> tuple[AnswerRow, ...]:
    question = manifest.questions[number - 1]
    scenario = manifest.scenario.rounds[number - 1]
    open_at = _open_at(manifest, number)
    rows: list[AnswerRow] = []
    for vu_id in expected_answering_vu_ids(scenario.answer_population):
        label = expected_virtual_option(vu_id)
        option = question.options[("A", "B", "C", "D").index(label)]
        participant_id = UUID(int=vu_id)
        rows.append(
            AnswerRow(
                answer_id=uuid5(RUN_ID, f"answer:{number}:{participant_id}"),
                participant_id=participant_id,
                participant_event_id=EVENT_ID,
                round_id=_round_id(manifest, number),
                option_id=option.id,
                option_question_id=question.id,
                option_label=label,
                option_is_correct=label == "B",
                client_answer_id=expected_virtual_client_answer_id(RUN_ID, question.id, vu_id),
                received_at=open_at + timedelta(milliseconds=10 + vu_id % 400),
                is_ranking_eligible=True,
            )
        )
    return tuple(rows)


def _ledgers(
    manifest: LoadTestManifest,
    number: int,
    answers: tuple[AnswerRow, ...],
) -> tuple[ScoreLedgerRow, ...]:
    policy = manifest.event.scoring_policy
    expected = score_round_independently(
        OracleScoringPolicy(
            version=policy.version,
            base_points=policy.base_points,
            initial_speed_bonus=policy.initial_speed_bonus,
            speed_bucket_ms=policy.speed_bucket_ms,
            speed_decrement_per_bucket=policy.speed_decrement_per_bucket,
            standard_duration_ms=policy.standard_duration_ms,
        ),
        tuple(
            OracleAnswer(
                participant_id=row.participant_id,
                is_correct=row.option_is_correct,
                is_ranking_eligible=row.is_ranking_eligible,
                open_at=_open_at(manifest, number),
                received_at=row.received_at,
            )
            for row in answers
        ),
    )
    return tuple(
        ScoreLedgerRow(
            score_ledger_id=uuid5(RUN_ID, f"ledger:{number}:{row.participant_id}"),
            round_id=_round_id(manifest, number),
            participant_id=row.participant_id,
            scoring_policy_version=row.scoring_policy_version,
            is_correct=row.is_correct,
            base_points=row.base_points,
            speed_points=row.speed_points,
            total_points=row.total_points,
            elapsed_ms=row.elapsed_ms,
            speed_bucket=row.speed_bucket,
            speed_rank=row.speed_rank,
            speed_cohort_size=row.speed_cohort_size,
            speed_cohort=row.speed_cohort,
            exclusion_reason=row.exclusion_reason,
        )
        for row in expected
    )


def _stats(manifest: LoadTestManifest, number: int) -> tuple[OptionStatRow, ...]:
    question = manifest.questions[number - 1]
    counts = (126, 125, 125, 124)
    return tuple(
        OptionStatRow(
            round_id=_round_id(manifest, number),
            option_id=option.id,
            option_question_id=question.id,
            option_label=option.label,
            display_order=option.display_order,
            option_is_correct=option.label == "B",
            valid_response_count=count,
            response_percentage=Decimal(count) / Decimal(5),
        )
        for option, count in zip(question.options, counts, strict=True)
    )


def _ranking(
    answers: tuple[AnswerRow, ...],
    ledgers: tuple[ScoreLedgerRow, ...],
    *,
    round_id: UUID,
    version: int,
) -> tuple[RankingConsistencyRow, ...]:
    participants = tuple(
        OracleParticipant(row.participant_id, row.is_ranking_eligible)
        for row in {answer.participant_id: answer for answer in answers}.values()
    )
    expected = rank_overall_independently(
        participants,
        tuple(
            OracleHistoricalLedger(
                row.round_id,
                row.participant_id,
                row.total_points,
                row.is_correct,
                row.elapsed_ms,
            )
            for row in ledgers
        ),
    )
    snapshot_id = uuid5(RUN_ID, f"snapshot:{version}")
    return tuple(
        RankingConsistencyRow(
            participant_id=row.participant_id,
            snapshot_id=snapshot_id,
            snapshot_version=version,
            snapshot_round_id=round_id,
            match_state="expected-and-stored",
            stored_overall_rank=row.overall_rank,
            expected_overall_rank=row.overall_rank,
            stored_total_points=row.total_points,
            expected_total_points=row.total_points,
            stored_correct_count=row.correct_count,
            expected_correct_count=row.correct_count,
        )
        for row in expected
    )


def _audit(
    manifest: LoadTestManifest,
    number: int,
    operation: str,
    index: int,
) -> OperationAuditRow:
    states = {
        "begin_preparation": ("PreparingAssets", "PreparingAssets"),
        "schedule_round": ("ReadyToSchedule", "Scheduled"),
        "aggregate_round_results": ("AwaitingValidation", "ReadyToReveal"),
        "publish_round_results": ("ReadyToReveal", "Reveal"),
        "validate_round_backup": ("RoundBackupPending", "RoundBackupValidated"),
        "advance_round": ("Reveal", "RoundBackupValidated"),
        "complete_event": ("InProgress", "Completed"),
    }
    key = (
        None
        if operation in {"validate_round_backup", "aggregate_round_results"}
        else controller_idempotency_key(RUN_ID, number, cast(object, operation))
    )
    before, after = states[operation]
    return OperationAuditRow(
        audit_id=uuid5(RUN_ID, f"audit:{number}:{operation}"),
        event_id=EVENT_ID,
        round_id=_round_id(manifest, number),
        account_id=None if operation == "aggregate_round_results" else OPERATOR_ID,
        operation=operation,
        is_automatic_aggregation=operation == "aggregate_round_results",
        before_state=before,
        after_state=after,
        outcome="succeeded",
        idempotency_key=key,
        occurred_at=_open_at(manifest, number) + timedelta(seconds=index),
    )


def _audits(
    manifest: LoadTestManifest, number: int, *, final: bool
) -> tuple[OperationAuditRow, ...]:
    operations = [
        "begin_preparation",
        "schedule_round",
        "aggregate_round_results",
        "publish_round_results",
    ]
    if final:
        operations.extend(
            [
                "validate_round_backup",
                "complete_event" if number == 10 else "advance_round",
            ]
        )
    return tuple(
        _audit(manifest, number, operation, index) for index, operation in enumerate(operations)
    )


def _idempotency(
    audits: tuple[OperationAuditRow, ...],
) -> tuple[IdempotencyRecordRow, ...]:
    return tuple(
        IdempotencyRecordRow(
            record_id=uuid5(RUN_ID, f"record:{row.idempotency_key}"),
            account_id=OPERATOR_ID,
            idempotency_key=cast(UUID, row.idempotency_key),
            operation=row.operation,
            response_status=200,
            resulting_version=1,
            created_at=row.occurred_at,
            expires_at=row.occurred_at + timedelta(hours=24),
        )
        for row in audits
        if row.idempotency_key is not None
    )


def _round_results(
    manifest: LoadTestManifest, number: int
) -> dict[str, tuple[StrictDbVerifierModel, ...]]:
    open_at = _open_at(manifest, number)
    round_id = _round_id(manifest, number)
    answers = _answers(manifest, number)
    ledgers = _ledgers(manifest, number, answers)
    audits = _audits(manifest, number, final=False)
    return {
        "round_identity": (
            RoundIdentityRow(
                event_id=EVENT_ID,
                event_version=number,
                event_state="InProgress",
                ranking_visibility="HiddenAll",
                round_id=round_id,
                round_version=number * 10 - 2,
                round_state="Reveal",
                result_publication="Published",
                question_id=manifest.questions[number - 1].id,
                question_version=number,
                open_at=open_at,
                standard_close_at=open_at + timedelta(seconds=10),
                revealed_at=open_at + timedelta(seconds=16),
            ),
        ),
        "scoring_policy": (_policy(manifest),),
        "readiness_summary": (
            ReadinessSummaryRow(
                round_id=round_id,
                round_version=number * 10 - 2,
                target_count=600,
                report_count=600,
                ready_count=600,
                asset_failure_count=0,
                stale_report_count=0,
                non_target_report_count=0,
            ),
        ),
        "answers": answers,
        "score_ledgers": ledgers,
        "option_stats": _stats(manifest, number),
        "ranking_consistency": _ranking(answers, ledgers, round_id=round_id, version=number),
        "operation_audits": audits,
        "idempotency_records": _idempotency(audits),
    }


def _final_results(
    manifest: LoadTestManifest,
) -> dict[str, tuple[StrictDbVerifierModel, ...]]:
    round_results = [_round_results(manifest, number) for number in range(1, 11)]
    answers = tuple(cast(AnswerRow, row) for result in round_results for row in result["answers"])
    ledgers = tuple(
        cast(ScoreLedgerRow, row) for result in round_results for row in result["score_ledgers"]
    )
    stats = tuple(
        cast(OptionStatRow, row) for result in round_results for row in result["option_stats"]
    )
    final_rounds = tuple(
        FinalRoundRow(
            round_id=(identity := cast(RoundIdentityRow, result["round_identity"][0])).round_id,
            round_version=identity.round_version + 2,
            round_state="RoundBackupValidated",
            result_publication="Published",
            question_id=identity.question_id,
            question_version=identity.question_version,
            open_at=identity.open_at,
            standard_close_at=identity.standard_close_at,
            revealed_at=identity.revealed_at,
        )
        for result in round_results
    )
    readiness = tuple(
        cast(ReadinessSummaryRow, result["readiness_summary"][0]).model_copy(
            update={"round_version": final_rounds[index].round_version}
        )
        for index, result in enumerate(round_results)
    )
    audits = tuple(row for number in range(1, 11) for row in _audits(manifest, number, final=True))
    return {
        "final_event": (
            FinalEventRow(
                event_id=EVENT_ID,
                event_version=100,
                event_state="Completed",
                ranking_visibility="HiddenAll",
                scoring_policy_version=manifest.event.scoring_policy.version,
                display_name_policy_version=manifest.event.display_name_policy.version,
            ),
        ),
        "final_scoring_policy": (_policy(manifest),),
        "final_rounds": final_rounds,
        "final_readiness_summaries": readiness,
        "final_answers": answers,
        "final_score_ledgers": ledgers,
        "final_option_stats": stats,
        "final_participant_summary": (
            FinalParticipantSummaryRow(
                participant_count=600,
                distinct_participant_id_count=600,
                ranking_eligible_count=600,
                session_count=600,
                distinct_session_participant_count=600,
                active_session_count=600,
                revoked_session_count=0,
            ),
        ),
        "final_integrity_summary": (
            FinalIntegritySummaryRow(
                round_count=10,
                distinct_round_id_count=10,
                distinct_question_id_count=10,
                preparation_target_count=6_000,
                distinct_preparation_target_count=6_000,
                readiness_report_count=6_000,
                distinct_readiness_report_count=6_000,
                answer_count=5_000,
                distinct_answer_id_count=5_000,
                distinct_answer_participant_round_count=5_000,
                distinct_client_answer_id_count=5_000,
                score_ledger_count=5_000,
                distinct_score_participant_round_count=5_000,
                option_stat_count=40,
                distinct_option_stat_count=40,
                rank_snapshot_count=10,
                distinct_snapshot_round_count=10,
                rank_snapshot_row_count=5_016,
            ),
        ),
        "final_ranking_consistency": _ranking(
            answers,
            ledgers,
            round_id=_round_id(manifest, 10),
            version=10,
        ),
        "final_operation_audits": audits,
        "final_idempotency_records": _idempotency(audits),
    }


class _FakeDatabase:
    def __init__(self, rows: Mapping[str, tuple[StrictDbVerifierModel, ...]]) -> None:
        self.rows = rows
        self.snapshot_revisions: list[str] = []

    @contextmanager
    def read_only_snapshot(self, *, expected_schema_revision: str) -> Iterator[_FakeDatabase]:
        self.snapshot_revisions.append(expected_schema_revision)
        yield self

    def execute_query(
        self,
        spec: DbQuerySpec[StrictDbVerifierModel],
        _parameters: Mapping[str, UUID],
    ) -> tuple[StrictDbVerifierModel, ...]:
        return self.rows[spec.query_id]


def _directory(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _loaded(value: BaseModel) -> LoadedDbVerifierRequest:
    return LoadedDbVerifierRequest(value=cast(object, value), artifact=_artifact("input.json", "3"))
