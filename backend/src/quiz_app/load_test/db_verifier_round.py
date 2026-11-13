from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeVar, cast
from uuid import UUID

from quiz_app.load_test.controller_contracts import (
    ControllerIdempotencyOperation,
    controller_idempotency_key,
)
from quiz_app.load_test.controller_verification import RoundVerificationRequest
from quiz_app.load_test.db_verifier_contracts import (
    PerRoundVerificationInput,
    QueryExecutionMetadata,
    StrictDbVerifierModel,
)
from quiz_app.load_test.db_verifier_models import (
    AnswerCheck,
    DbVerifierError,
    LoadedDbVerifierRequest,
    PerRoundVerificationReport,
    RoundVerificationFacts,
)
from quiz_app.load_test.db_verifier_oracle import (
    DbVerifierOracleError,
    OracleAnswer,
    OracleScoreLedger,
    OracleScoringPolicy,
    expected_answering_vu_ids,
    expected_virtual_client_answer_id,
    expected_virtual_option,
    score_round_independently,
)
from quiz_app.load_test.db_verifier_queries import (
    AnswerRow,
    IdempotencyRecordRow,
    OperationAuditRow,
    OptionStatRow,
    RankingConsistencyRow,
    ReadinessSummaryRow,
    RoundIdentityRow,
    ScoreLedgerRow,
    ScoringPolicyRow,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.verification_contracts import ArtifactIdentity

RowType = TypeVar("RowType", bound=StrictDbVerifierModel)


def build_per_round_report(
    manifest: LoadTestManifest,
    loaded: LoadedDbVerifierRequest,
    results: Mapping[str, tuple[StrictDbVerifierModel, ...]],
    executions: tuple[QueryExecutionMetadata, ...],
    sealed_at: datetime,
) -> PerRoundVerificationReport:
    """1 Round分のtyped query結果を独立oracleで検証する。"""

    value = cast(PerRoundVerificationInput, loaded.value)
    request = value.request
    identity = one(results, "round_identity", RoundIdentityRow)
    scoring_policy = one(results, "scoring_policy", ScoringPolicyRow)
    readiness = one(results, "readiness_summary", ReadinessSummaryRow)
    answers = rows(results, "answers", AnswerRow)
    ledgers = rows(results, "score_ledgers", ScoreLedgerRow)
    stats = rows(results, "option_stats", OptionStatRow)
    rankings = rows(results, "ranking_consistency", RankingConsistencyRow)
    audits = rows(results, "operation_audits", OperationAuditRow)
    idempotency = rows(results, "idempotency_records", IdempotencyRecordRow)

    failures: list[str] = []
    record(failures, "round_identity_mismatch", valid_round_identity(manifest, request, identity))
    record(failures, "scoring_policy_mismatch", valid_scoring_policy(manifest, scoring_policy))
    record(
        failures,
        "readiness_mismatch",
        valid_readiness(manifest, request.round_id, identity.round_version, readiness),
    )
    answer_check = check_round_answers(
        manifest, request.round_number, request.round_id, identity, answers
    )
    record(failures, "answers_mismatch", answer_check.is_valid)
    record(failures, "score_ledgers_mismatch", valid_ledgers(manifest, answer_check, ledgers))
    record(
        failures,
        "option_stats_mismatch",
        valid_option_stats(manifest, request.round_number, request.round_id, stats),
    )
    record(
        failures,
        "ranking_mismatch",
        valid_ranking(rankings, request.round_number, request.round_id),
    )
    record(
        failures,
        "operation_audits_mismatch",
        valid_round_audits(manifest, request.round_number, request.round_id, audits),
    )
    record(
        failures,
        "idempotency_records_mismatch",
        valid_round_idempotency(manifest, request.round_number, idempotency, audits),
    )

    facts = RoundVerificationFacts(
        preparation_target_count=readiness.target_count,
        readiness_report_count=readiness.report_count,
        ready_count=readiness.ready_count,
        asset_failure_count=readiness.asset_failure_count,
        answer_count=len(answers),
        virtual_answer_count=answer_check.virtual_count,
        score_ledger_count=len(ledgers),
        option_stat_count=len(stats),
        ranking_row_count=len(rankings),
        operation_audit_count=len(audits),
        idempotency_record_count=len(idempotency),
    )
    return PerRoundVerificationReport(
        format="load-test-db-round-verification-report-v2",
        mode="per-round",
        **report_binding(request, loaded.artifact, executions, failures, sealed_at),
        facts=facts,
    )


def check_round_answers(
    manifest: LoadTestManifest,
    round_number: int,
    round_id: UUID,
    identity: RoundIdentityRow,
    answers: tuple[AnswerRow, ...],
) -> AnswerCheck:
    """DB answerを600自動参加者のscenario由来UUIDv5で検証する。"""

    scenario = manifest.scenario.rounds[round_number - 1]
    question = manifest.questions[round_number - 1]
    expected_ids = {
        expected_virtual_client_answer_id(manifest.run_id, question.id, vu_id): vu_id
        for vu_id in expected_answering_vu_ids(scenario.answer_population)
    }
    expected_virtual_ids = set(expected_ids)
    seen_answers: set[UUID] = set()
    seen_participants: set[UUID] = set()
    seen_client_ids: set[UUID] = set()
    virtual_participants: dict[int, UUID] = {}
    score_inputs: list[OracleAnswer] = []
    is_valid = len(answers) == 500

    option_by_label = {option.label: option for option in question.options}
    for row in answers:
        vu_id = expected_ids.get(row.client_answer_id)
        if vu_id is None:
            is_valid = False
        else:
            virtual_participants[vu_id] = row.participant_id
            is_valid = is_valid and row.option_label == expected_virtual_option(vu_id)
        option = option_by_label.get(row.option_label)
        is_valid = is_valid and (
            row.answer_id not in seen_answers
            and row.participant_id not in seen_participants
            and row.client_answer_id not in seen_client_ids
            and row.participant_event_id == manifest.event.id
            and row.round_id == round_id
            and row.option_question_id == question.id
            and option is not None
            and row.option_id == option.id
            and row.option_is_correct == (row.option_label == "B")
            and row.is_ranking_eligible
            and row.client_elapsed_ms is None
            and identity.open_at <= row.received_at < identity.standard_close_at
        )
        seen_answers.add(row.answer_id)
        seen_participants.add(row.participant_id)
        seen_client_ids.add(row.client_answer_id)
        score_inputs.append(
            OracleAnswer(
                participant_id=row.participant_id,
                is_correct=row.option_is_correct,
                is_ranking_eligible=row.is_ranking_eligible,
                open_at=identity.open_at,
                received_at=row.received_at,
            )
        )

    actual_virtual_ids = seen_client_ids.intersection(expected_ids)
    is_valid = is_valid and actual_virtual_ids == expected_virtual_ids
    return AnswerCheck(
        is_valid=is_valid,
        virtual_count=len(actual_virtual_ids),
        score_inputs=tuple(score_inputs),
        virtual_participants=virtual_participants,
    )


def valid_ledgers(
    manifest: LoadTestManifest,
    answers: AnswerCheck,
    stored: tuple[ScoreLedgerRow, ...],
) -> bool:
    """production scoringをimportせず、独立oracleと全ledgerを照合する。"""

    try:
        expected = score_round_independently(scoring_policy(manifest), answers.score_inputs)
    except DbVerifierOracleError:
        return False
    if len(expected) != len(stored) or len({row.score_ledger_id for row in stored}) != len(stored):
        return False
    stored_by_participant = {row.participant_id: row for row in stored}
    if len(stored_by_participant) != len(stored):
        return False
    return all(
        (actual := stored_by_participant.get(row.participant_id)) is not None
        and _ledger_values(actual) == _oracle_ledger_values(row)
        for row in expected
    )


def valid_option_stats(
    manifest: LoadTestManifest,
    round_number: int,
    round_id: UUID,
    values: tuple[OptionStatRow, ...],
) -> bool:
    if len(values) != 4:
        return False
    question = manifest.questions[round_number - 1]
    scenario = manifest.scenario.rounds[round_number - 1]
    expected_counts = dict(
        zip(
            ("A", "B", "C", "D"),
            (
                scenario.expected_option_counts.a,
                scenario.expected_option_counts.b,
                scenario.expected_option_counts.c,
                scenario.expected_option_counts.d,
            ),
            strict=True,
        )
    )
    row_by_label = {row.option_label: row for row in values}
    if len(row_by_label) != 4:
        return False
    for option in question.options:
        row = row_by_label.get(option.label)
        count = expected_counts[option.label]
        expected_percentage = (Decimal(count) * Decimal(100) / Decimal(500)).quantize(
            Decimal("0.01")
        )
        if row is None or (
            row.round_id,
            row.option_id,
            row.option_question_id,
            row.display_order,
            row.option_is_correct,
            row.valid_response_count,
            row.response_percentage,
        ) != (
            round_id,
            option.id,
            question.id,
            option.display_order,
            option.label == "B",
            count,
            expected_percentage,
        ):
            return False
    return True


def valid_round_identity(
    manifest: LoadTestManifest,
    request: RoundVerificationRequest,
    row: RoundIdentityRow,
) -> bool:
    number = request.round_number
    question = manifest.questions[number - 1]
    scenario = manifest.scenario.rounds[number - 1]
    expected_open = manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)
    return (
        row.event_id == manifest.event.id == request.event_id
        and row.event_state == "InProgress"
        and row.ranking_visibility == "HiddenAll"
        and row.round_id == request.round_id
        and (
            (number == 1 and row.round_id == manifest.initial_round.id)
            or (number > 1 and row.round_id.version == 4)
        )
        and row.round_state == "Reveal"
        and row.result_publication == "Published"
        and row.question_id == question.id == request.question_id
        and row.question_version == number
        and row.open_at == expected_open
        and row.standard_close_at
        == expected_open
        + timedelta(milliseconds=manifest.event.scoring_policy.standard_duration_ms)
        and row.standard_close_at <= row.revealed_at <= request.requested_at
    )


def valid_scoring_policy(manifest: LoadTestManifest, row: ScoringPolicyRow) -> bool:
    # 旧manifestは端末計測や猶予を固定していないため、新方式の成功証跡に流用しない。
    if (
        row.timing_basis,
        row.arrival_grace_ms,
        row.clock_tolerance_ms,
        row.client_ahead_tolerance_ms,
    ) != (
        "server_received_v1",
        0,
        0,
        0,
    ):
        return False
    expected = manifest.event.scoring_policy
    return (
        row.scoring_policy_version,
        row.base_points,
        row.initial_speed_bonus,
        row.speed_bucket_ms,
        row.speed_decrement_per_bucket,
        row.standard_duration_ms,
        row.is_approved,
    ) == (
        expected.version,
        expected.base_points,
        expected.initial_speed_bonus,
        expected.speed_bucket_ms,
        expected.speed_decrement_per_bucket,
        expected.standard_duration_ms,
        True,
    )


def valid_readiness(
    manifest: LoadTestManifest,
    round_id: UUID,
    round_version: int,
    row: ReadinessSummaryRow,
) -> bool:
    thresholds = manifest.scenario.thresholds
    return (
        row.round_id == round_id
        and row.round_version == round_version
        and row.target_count == thresholds.target_count
        and row.report_count == thresholds.target_count
        and row.ready_count >= thresholds.ready_count_minimum
        and row.asset_failure_count <= thresholds.asset_failure_maximum
        and row.ready_count + row.asset_failure_count <= row.report_count
        and row.stale_report_count == 0
        and row.non_target_report_count == 0
    )


def valid_ranking(
    values: tuple[RankingConsistencyRow, ...],
    round_number: int,
    round_id: UUID,
) -> bool:
    expected_count = 500 if round_number <= 2 else 502
    participant_ids = [row.participant_id for row in values]
    snapshot_ids = {row.snapshot_id for row in values}
    return (
        len(values) == expected_count
        and None not in participant_ids
        and len(participant_ids) == len(set(participant_ids))
        and len(snapshot_ids) == 1
        and None not in snapshot_ids
        and all(
            row.match_state == "expected-and-stored"
            and row.snapshot_version == round_number
            and row.snapshot_round_id == round_id
            and row.stored_overall_rank == row.expected_overall_rank
            and row.stored_total_points == row.expected_total_points
            and row.stored_correct_count == row.expected_correct_count
            for row in values
        )
    )


def valid_round_audits(
    manifest: LoadTestManifest,
    round_number: int,
    round_id: UUID,
    values: tuple[OperationAuditRow, ...],
) -> bool:
    expected_operations = {
        "begin_preparation": ("PreparingAssets", "PreparingAssets"),
        "schedule_round": ("ReadyToSchedule", "Scheduled"),
        "aggregate_round_results": ("AwaitingValidation", "ReadyToReveal"),
        "publish_round_results": ("ReadyToReveal", "Reveal"),
    }
    if len(values) != 4 or len({row.audit_id for row in values}) != 4:
        return False
    by_operation = {row.operation: row for row in values}
    if set(by_operation) != set(expected_operations):
        return False
    account_ids = {row.account_id for row in values if row.operation != "aggregate_round_results"}
    if len(account_ids) != 1 or None in account_ids:
        return False
    for operation, states in expected_operations.items():
        row = by_operation[operation]
        is_automatic = operation == "aggregate_round_results"
        expected_key = (
            None
            if is_automatic
            else controller_idempotency_key(
                manifest.run_id, round_number, cast(ControllerIdempotencyOperation, operation)
            )
        )
        if (
            row.event_id != manifest.event.id
            or row.round_id != round_id
            or row.outcome != "succeeded"
            or row.idempotency_key != expected_key
            or (is_automatic and (row.account_id is not None or not row.is_automatic_aggregation))
            or (row.before_state, row.after_state) != states
        ):
            return False
    return nondecreasing(row.occurred_at for row in values)


def valid_round_idempotency(
    manifest: LoadTestManifest,
    round_number: int,
    values: tuple[IdempotencyRecordRow, ...],
    audits: tuple[OperationAuditRow, ...],
) -> bool:
    operations: tuple[ControllerIdempotencyOperation, ...] = (
        "begin_preparation",
        "schedule_round",
        "publish_round_results",
    )
    expected = {
        controller_idempotency_key(manifest.run_id, round_number, operation): operation
        for operation in operations
    }
    return valid_idempotency_rows(values, audits, expected)


def valid_idempotency_rows(
    values: tuple[IdempotencyRecordRow, ...],
    audits: tuple[OperationAuditRow, ...],
    expected: Mapping[UUID, str],
) -> bool:
    by_key = {row.idempotency_key: row for row in values}
    audit_keys = {
        row.idempotency_key: row.operation for row in audits if row.idempotency_key is not None
    }
    if (
        len(values) != len(expected)
        or len(by_key) != len(values)
        or len({row.record_id for row in values}) != len(values)
        or set(by_key) != set(expected)
        or audit_keys != dict(expected)
    ):
        return False
    account_ids = {row.account_id for row in values}
    audit_accounts = {row.account_id for row in audits if row.idempotency_key in expected}
    if len(account_ids) != 1 or None in account_ids or account_ids != audit_accounts:
        return False
    return all(
        row.operation == expected[row.idempotency_key]
        and row.response_status == 200
        and row.resulting_version >= 1
        and row.created_at < row.expires_at
        for row in values
    )


def scoring_policy(manifest: LoadTestManifest) -> OracleScoringPolicy:
    policy = manifest.event.scoring_policy
    return OracleScoringPolicy(
        version=policy.version,
        base_points=policy.base_points,
        initial_speed_bonus=policy.initial_speed_bonus,
        speed_bucket_ms=policy.speed_bucket_ms,
        speed_decrement_per_bucket=policy.speed_decrement_per_bucket,
        standard_duration_ms=policy.standard_duration_ms,
    )


def report_binding(
    request: RoundVerificationRequest,
    input_artifact: ArtifactIdentity,
    executions: tuple[QueryExecutionMetadata, ...],
    failures: Sequence[str],
    sealed_at: datetime,
) -> dict[str, object]:
    unique_failures = tuple(dict.fromkeys(failures))
    return {
        "verification_id": request.verification_id,
        "run_id": request.run_id,
        "event_id": request.event_id,
        "source_commit": request.source_commit,
        "schema_revision": request.schema_revision,
        "manifest_sha256": request.manifest_sha256,
        "scenario_sha256": request.scenario_sha256,
        "round_number": request.round_number,
        "round_id": request.round_id,
        "question_id": request.question_id,
        "verification_requested_head_sha256": request.verification_requested_head_sha256,
        "generator_evidence": request.generator_evidence,
        "input_artifact": input_artifact,
        "query_executions": executions,
        "decision": "PASS" if not unique_failures else "FAIL",
        "failure_codes": unique_failures,
        "sealed_at": sealed_at,
    }


def rows(
    results: Mapping[str, tuple[StrictDbVerifierModel, ...]],
    query_id: str,
    row_type: type[RowType],
) -> tuple[RowType, ...]:
    values = results.get(query_id)
    if values is None or any(not isinstance(row, row_type) for row in values):
        raise DbVerifierError("db_verifier_query_result_invalid")
    return cast(tuple[RowType, ...], values)


def one(
    results: Mapping[str, tuple[StrictDbVerifierModel, ...]],
    query_id: str,
    row_type: type[RowType],
) -> RowType:
    values = rows(results, query_id, row_type)
    if len(values) != 1:
        raise DbVerifierError("db_verifier_query_result_invalid")
    return values[0]


def record(failures: list[str], code: str, condition: bool) -> None:
    if not condition:
        failures.append(code)


def nondecreasing(values: Iterable[datetime]) -> bool:
    ordered = tuple(values)
    return all(left <= right for left, right in zip(ordered, ordered[1:], strict=False))


def _ledger_values(row: ScoreLedgerRow) -> tuple[object, ...]:
    return (
        row.scoring_policy_version,
        row.is_correct,
        row.base_points,
        row.speed_points,
        row.total_points,
        row.elapsed_ms,
        row.speed_bucket,
        row.speed_rank,
        row.speed_cohort_size,
        row.speed_cohort,
        row.exclusion_reason,
    )


def _oracle_ledger_values(row: OracleScoreLedger) -> tuple[object, ...]:
    return (
        row.scoring_policy_version,
        row.is_correct,
        row.base_points,
        row.speed_points,
        row.total_points,
        row.elapsed_ms,
        row.speed_bucket,
        row.speed_rank,
        row.speed_cohort_size,
        row.speed_cohort,
        row.exclusion_reason,
    )
