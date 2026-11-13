from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from quiz_app.load_test.controller_contracts import (
    ControllerIdempotencyOperation,
    controller_idempotency_key,
)
from quiz_app.load_test.db_verifier_contracts import (
    FinalVerificationInput,
    QueryExecutionMetadata,
    StrictDbVerifierModel,
)
from quiz_app.load_test.db_verifier_models import (
    AnswerCheck,
    FinalVerificationFacts,
    FinalVerificationReport,
)
from quiz_app.load_test.db_verifier_oracle import (
    DbVerifierOracleError,
    OracleHistoricalLedger,
    OracleParticipant,
    expected_answering_vu_ids,
    rank_overall_independently,
)
from quiz_app.load_test.db_verifier_queries import (
    AnswerRow,
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
from quiz_app.load_test.db_verifier_round import (
    check_round_answers,
    nondecreasing,
    one,
    record,
    report_binding,
    rows,
    valid_idempotency_rows,
    valid_ledgers,
    valid_option_stats,
    valid_ranking,
    valid_readiness,
    valid_scoring_policy,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.verification_contracts import ArtifactIdentity


def build_final_report(
    manifest: LoadTestManifest,
    value: FinalVerificationInput,
    input_artifact: ArtifactIdentity,
    results: Mapping[str, tuple[StrictDbVerifierModel, ...]],
    executions: tuple[QueryExecutionMetadata, ...],
    sealed_at: datetime,
) -> FinalVerificationReport:
    """10 Round全体の固定集計と独立rankingを再計算する。"""

    request = value.request
    event = one(results, "final_event", FinalEventRow)
    scoring_policy = one(results, "final_scoring_policy", ScoringPolicyRow)
    final_rounds = rows(results, "final_rounds", FinalRoundRow)
    participants = one(results, "final_participant_summary", FinalParticipantSummaryRow)
    integrity = one(results, "final_integrity_summary", FinalIntegritySummaryRow)
    readiness = rows(results, "final_readiness_summaries", ReadinessSummaryRow)
    answers = rows(results, "final_answers", AnswerRow)
    ledgers = rows(results, "final_score_ledgers", ScoreLedgerRow)
    stats = rows(results, "final_option_stats", OptionStatRow)
    rankings = rows(results, "final_ranking_consistency", RankingConsistencyRow)
    audits = rows(results, "final_operation_audits", OperationAuditRow)
    idempotency = rows(results, "final_idempotency_records", IdempotencyRecordRow)

    failures: list[str] = []
    record(failures, "final_event_mismatch", _valid_final_event(manifest, event))
    record(
        failures,
        "final_scoring_policy_mismatch",
        valid_scoring_policy(manifest, scoring_policy),
    )
    record(
        failures,
        "final_rounds_mismatch",
        _valid_final_rounds(manifest, request.round_id, final_rounds),
    )
    round_by_id = {row.round_id: row for row in final_rounds}
    readiness_valid = (
        len(readiness) == 10
        and {row.round_id for row in readiness} == set(round_by_id)
        and all(
            row.round_id in round_by_id
            and valid_readiness(
                manifest,
                row.round_id,
                round_by_id[row.round_id].round_version,
                row,
            )
            for row in readiness
        )
    )
    record(failures, "final_readiness_mismatch", readiness_valid)

    answer_checks: list[AnswerCheck] = []
    final_answers_valid = len(final_rounds) == 10
    for number, round_row in enumerate(final_rounds, start=1):
        round_answers = tuple(row for row in answers if row.round_id == round_row.round_id)
        identity = RoundIdentityRow(
            event_id=manifest.event.id,
            event_version=event.event_version,
            event_state="InProgress",
            ranking_visibility="HiddenAll",
            round_id=round_row.round_id,
            round_version=round_row.round_version,
            round_state="Reveal",
            result_publication="Published",
            question_id=round_row.question_id,
            question_version=round_row.question_version,
            open_at=round_row.open_at,
            standard_close_at=round_row.standard_close_at,
            revealed_at=round_row.revealed_at,
        )
        check = check_round_answers(
            manifest,
            number,
            round_row.round_id,
            identity,
            round_answers,
        )
        answer_checks.append(check)
        final_answers_valid = final_answers_valid and check.is_valid
    final_answers_valid = final_answers_valid and _valid_cross_round_population(
        manifest,
        answer_checks,
        answers,
    )
    record(failures, "final_answers_mismatch", final_answers_valid and len(answers) == 5_000)

    record(
        failures,
        "final_score_ledgers_mismatch",
        _valid_final_ledgers(manifest, answers, answer_checks, ledgers, final_rounds),
    )
    stats_valid = len(stats) == 40 and all(
        valid_option_stats(
            manifest,
            round_row.question_version,
            round_row.round_id,
            tuple(row for row in stats if row.round_id == round_row.round_id),
        )
        for round_row in final_rounds
    )
    record(failures, "final_option_stats_mismatch", stats_valid)
    record(
        failures,
        "final_ranking_mismatch",
        _valid_final_ranking(rankings, answers, ledgers, final_rounds, request.round_id),
    )
    record(
        failures,
        "final_participants_mismatch",
        _valid_final_participants(participants, answers),
    )
    record(failures, "final_integrity_mismatch", _valid_final_integrity(integrity))
    record(
        failures,
        "final_operation_audits_mismatch",
        _valid_final_audits(manifest, final_rounds, audits),
    )
    record(
        failures,
        "final_idempotency_records_mismatch",
        _valid_final_idempotency(manifest, idempotency, audits),
    )

    virtual_count = sum(check.virtual_count for check in answer_checks)
    experienced = len({row.participant_id for row in answers})
    audit_counts = Counter(row.operation for row in audits)
    facts = FinalVerificationFacts(
        participant_count=participants.participant_count,
        preparation_target_count=integrity.preparation_target_count,
        readiness_report_count=integrity.readiness_report_count,
        answer_count=integrity.answer_count,
        score_ledger_count=integrity.score_ledger_count,
        virtual_answer_count=virtual_count,
        option_stat_count=integrity.option_stat_count,
        rank_snapshot_count=integrity.rank_snapshot_count,
        rank_snapshot_row_count=integrity.rank_snapshot_row_count,
        experienced_participant_count=experienced,
        nonresponder_count=max(0, participants.participant_count - experienced),
        idempotency_record_count=len(idempotency),
        preparation_audit_count=audit_counts["begin_preparation"],
        schedule_audit_count=audit_counts["schedule_round"],
        aggregation_audit_count=audit_counts["aggregate_round_results"],
        publish_audit_count=audit_counts["publish_round_results"],
        advance_audit_count=audit_counts["advance_round"],
        complete_audit_count=audit_counts["complete_event"],
        backup_validation_audit_count=audit_counts["validate_round_backup"],
    )
    return FinalVerificationReport(
        format="load-test-db-final-verification-report-v2",
        mode="final",
        round_identity_scope=(
            "round-1-and-round-10-exact;round-2-through-9-unique-and-question-bound"
        ),
        **report_binding(request, input_artifact, executions, failures, sealed_at),
        facts=facts,
    )


def _valid_final_ledgers(
    manifest: LoadTestManifest,
    answer_rows: tuple[AnswerRow, ...],
    answer_checks: Sequence[AnswerCheck],
    stored: tuple[ScoreLedgerRow, ...],
    final_rounds: tuple[FinalRoundRow, ...],
) -> bool:
    if len(answer_rows) != 5_000 or len(stored) != 5_000 or len(answer_checks) != 10:
        return False
    stored_by_round: dict[UUID, list[ScoreLedgerRow]] = defaultdict(list)
    for row in stored:
        stored_by_round[row.round_id].append(row)
    if set(stored_by_round) != {row.round_id for row in final_rounds}:
        return False
    return all(
        valid_ledgers(
            manifest,
            answer_checks[index],
            tuple(stored_by_round[round_row.round_id]),
        )
        for index, round_row in enumerate(final_rounds)
    )


def _valid_final_event(manifest: LoadTestManifest, row: FinalEventRow) -> bool:
    return (
        row.event_id == manifest.event.id
        and row.event_state == "Completed"
        and row.ranking_visibility == "HiddenAll"
        and row.scoring_policy_version == manifest.event.scoring_policy.version
        and row.display_name_policy_version == manifest.event.display_name_policy.version
    )


def _valid_final_rounds(
    manifest: LoadTestManifest,
    final_round_id: UUID,
    values: tuple[FinalRoundRow, ...],
) -> bool:
    if (
        len(values) != 10
        or tuple(row.question_version for row in values) != tuple(range(1, 11))
        or len({row.round_id for row in values}) != 10
        or values[0].round_id != manifest.initial_round.id
        or values[-1].round_id != final_round_id
        or any(row.round_id.version != 4 for row in values[1:])
        or any(
            left.round_version >= right.round_version
            for left, right in zip(values, values[1:], strict=False)
        )
    ):
        return False
    for number, row in enumerate(values, start=1):
        scenario = manifest.scenario.rounds[number - 1]
        expected_open = manifest.scheduled_start_at + timedelta(
            seconds=scenario.start_offset_seconds
        )
        if (
            row.question_id != manifest.questions[number - 1].id
            or row.round_version < 1
            or row.round_state != "RoundBackupValidated"
            or row.result_publication != "Published"
            or row.open_at != expected_open
            or row.standard_close_at
            != expected_open
            + timedelta(milliseconds=manifest.event.scoring_policy.standard_duration_ms)
            or row.revealed_at < row.standard_close_at
        ):
            return False
    return True


def _valid_final_participants(
    row: FinalParticipantSummaryRow,
    answers: tuple[AnswerRow, ...],
) -> bool:
    experienced = len({answer.participant_id for answer in answers})
    return (
        row.participant_count == 600
        and row.distinct_participant_id_count == 600
        and row.ranking_eligible_count == 600
        and row.session_count == 600
        and row.distinct_session_participant_count == 600
        and row.active_session_count == 600
        and row.revoked_session_count == 0
        and experienced == 502
        and row.participant_count - experienced == 98
    )


def _valid_final_integrity(row: FinalIntegritySummaryRow) -> bool:
    return (
        row.round_count,
        row.distinct_round_id_count,
        row.distinct_question_id_count,
        row.preparation_target_count,
        row.distinct_preparation_target_count,
        row.readiness_report_count,
        row.distinct_readiness_report_count,
        row.answer_count,
        row.distinct_answer_id_count,
        row.distinct_answer_participant_round_count,
        row.distinct_client_answer_id_count,
        row.score_ledger_count,
        row.distinct_score_participant_round_count,
        row.option_stat_count,
        row.distinct_option_stat_count,
        row.rank_snapshot_count,
        row.distinct_snapshot_round_count,
        row.rank_snapshot_row_count,
    ) == (
        10,
        10,
        10,
        6_000,
        6_000,
        6_000,
        6_000,
        5_000,
        5_000,
        5_000,
        5_000,
        5_000,
        5_000,
        40,
        40,
        10,
        10,
        5_016,
    )


def _valid_final_audits(
    manifest: LoadTestManifest,
    final_rounds: tuple[FinalRoundRow, ...],
    values: tuple[OperationAuditRow, ...],
) -> bool:
    expected_counts = {
        "begin_preparation": 10,
        "schedule_round": 10,
        "aggregate_round_results": 10,
        "publish_round_results": 10,
        "advance_round": 9,
        "complete_event": 1,
        "validate_round_backup": 10,
    }
    expected_states = {
        "begin_preparation": ("PreparingAssets", "PreparingAssets"),
        "schedule_round": ("ReadyToSchedule", "Scheduled"),
        "aggregate_round_results": ("AwaitingValidation", "ReadyToReveal"),
        "publish_round_results": ("ReadyToReveal", "Reveal"),
        "advance_round": ("Reveal", "RoundBackupValidated"),
        "complete_event": ("InProgress", "Completed"),
        "validate_round_backup": ("RoundBackupPending", "RoundBackupValidated"),
    }
    if (
        len(values) != 60
        or Counter(row.operation for row in values) != expected_counts
        or len({row.audit_id for row in values}) != 60
        or not nondecreasing(row.occurred_at for row in values)
    ):
        return False
    round_number_by_id = {row.round_id: number for number, row in enumerate(final_rounds, start=1)}
    account_ids = {row.account_id for row in values if row.operation != "aggregate_round_results"}
    if len(account_ids) != 1 or None in account_ids:
        return False
    for row in values:
        number = round_number_by_id.get(row.round_id)
        if (
            number is None
            or row.event_id != manifest.event.id
            or row.outcome != "succeeded"
            or (row.before_state, row.after_state) != expected_states.get(row.operation)
        ):
            return False
        if row.operation == "aggregate_round_results":
            if (
                row.account_id is not None
                or row.idempotency_key is not None
                or not row.is_automatic_aggregation
            ):
                return False
            continue
        if row.operation == "validate_round_backup":
            if row.idempotency_key is not None:
                return False
            continue
        try:
            expected_key = controller_idempotency_key(
                manifest.run_id,
                number,
                cast(ControllerIdempotencyOperation, row.operation),
            )
        except ValueError:
            return False
        if row.idempotency_key != expected_key:
            return False
    return True


def _valid_final_idempotency(
    manifest: LoadTestManifest,
    values: tuple[IdempotencyRecordRow, ...],
    audits: tuple[OperationAuditRow, ...],
) -> bool:
    expected: dict[UUID, str] = {}
    for number in range(1, 11):
        operations: list[ControllerIdempotencyOperation] = [
            "begin_preparation",
            "schedule_round",
            "publish_round_results",
        ]
        operations.append("complete_event" if number == 10 else "advance_round")
        for operation in operations:
            expected[controller_idempotency_key(manifest.run_id, number, operation)] = operation
    return len(expected) == 40 and valid_idempotency_rows(values, audits, expected)


def _valid_cross_round_population(
    manifest: LoadTestManifest,
    checks: Sequence[AnswerCheck],
    answers: tuple[AnswerRow, ...],
) -> bool:
    if len(checks) != 10:
        return False
    participants_by_vu: dict[int, set[UUID]] = defaultdict(set)
    observed_rounds_by_vu: Counter[int] = Counter()
    for check in checks:
        for vu_id, participant_id in check.virtual_participants.items():
            participants_by_vu[vu_id].add(participant_id)
            observed_rounds_by_vu[vu_id] += 1
    expected_rounds_by_vu = Counter(
        vu_id
        for scenario in manifest.scenario.rounds
        for vu_id in expected_answering_vu_ids(scenario.answer_population)
    )
    return (
        set(participants_by_vu) == set(expected_rounds_by_vu)
        and all(len(values) == 1 for values in participants_by_vu.values())
        and observed_rounds_by_vu == expected_rounds_by_vu
        and len({next(iter(values)) for values in participants_by_vu.values()}) == 502
        and len({row.participant_id for row in answers}) == 502
        and sum(check.virtual_count for check in checks) == 5_000
    )


def _valid_final_ranking(
    values: tuple[RankingConsistencyRow, ...],
    answers: tuple[AnswerRow, ...],
    ledgers: tuple[ScoreLedgerRow, ...],
    final_rounds: tuple[FinalRoundRow, ...],
    final_round_id: UUID,
) -> bool:
    if not valid_ranking(values, 10, final_round_id) or len(final_rounds) != 10:
        return False
    participant_flags: dict[UUID, bool] = {}
    for answer in answers:
        flags = answer.is_ranking_eligible
        existing = participant_flags.setdefault(answer.participant_id, flags)
        if existing != flags:
            return False
    participants = tuple(
        OracleParticipant(
            participant_id=participant_id,
            is_ranking_eligible=flags,
        )
        for participant_id, flags in participant_flags.items()
    )
    round_ids = {row.round_id for row in final_rounds}
    if any(row.round_id not in round_ids for row in ledgers):
        return False
    historical = tuple(
        OracleHistoricalLedger(
            round_id=row.round_id,
            participant_id=row.participant_id,
            total_points=row.total_points,
            is_correct=row.is_correct,
            elapsed_ms=row.elapsed_ms,
        )
        for row in ledgers
    )
    try:
        expected = rank_overall_independently(participants, historical)
    except DbVerifierOracleError:
        return False
    stored_by_participant = {
        row.participant_id: row for row in values if row.participant_id is not None
    }
    return len(expected) == 502 and all(
        (stored := stored_by_participant.get(expected_row.participant_id)) is not None
        and (
            stored.stored_overall_rank,
            stored.stored_total_points,
            stored.stored_correct_count,
            stored.expected_overall_rank,
            stored.expected_total_points,
            stored.expected_correct_count,
        )
        == (
            expected_row.overall_rank,
            expected_row.total_points,
            expected_row.correct_count,
            expected_row.overall_rank,
            expected_row.total_points,
            expected_row.correct_count,
        )
        for expected_row in expected
    )
