from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from quiz_app.load_test.controller_contracts import manifest_context, verification_id
from quiz_app.load_test.controller_verification import (
    VerificationHandoffError,
    load_generator_evidence,
)
from quiz_app.load_test.db_verifier_artifacts import (
    load_db_verifier_request,
    seal_bound_report,
)
from quiz_app.load_test.db_verifier_contracts import (
    LOCK_OBSERVATION_DURATION_MS,
    DbVerifierRequest,
    FinalVerificationInput,
    LockObservationInput,
    PerRoundVerificationInput,
    QueryExecutionMetadata,
    StrictDbVerifierModel,
)
from quiz_app.load_test.db_verifier_database import VerifierDatabase
from quiz_app.load_test.db_verifier_final import build_final_report
from quiz_app.load_test.db_verifier_locks import aware_now, observe_locks
from quiz_app.load_test.db_verifier_models import (
    Clock,
    DbVerifierError,
    DbVerifierReport,
    DbVerifierRunResult,
    FinalVerificationFacts,
    FinalVerificationReport,
    LoadedDbVerifierRequest,
    LockObservationFacts,
    LockObservationReport,
    PerRoundVerificationReport,
    RoundVerificationFacts,
    Sleeper,
)
from quiz_app.load_test.db_verifier_queries import query_specs_for_mode
from quiz_app.load_test.db_verifier_round import build_per_round_report
from quiz_app.load_test.manifest import LoadTestManifest

__all__ = [
    "DbVerifierError",
    "DbVerifierReport",
    "DbVerifierRunResult",
    "FinalVerificationFacts",
    "FinalVerificationReport",
    "LoadedDbVerifierRequest",
    "LockObservationFacts",
    "LockObservationReport",
    "PerRoundVerificationReport",
    "RoundVerificationFacts",
    "load_db_verifier_request",
    "run_db_verifier",
]


def run_db_verifier(
    manifest: LoadTestManifest,
    loaded: LoadedDbVerifierRequest,
    database: VerifierDatabase,
    output_directory: Path,
    *,
    generator_evidence_path: Path | None = None,
    clock: Clock = lambda: datetime.now(UTC),
    sleeper: Sleeper = time.sleep,
) -> DbVerifierRunResult:
    """modeごとのfixed queryを実行し、FAIL reportまたはPASS receiptをsealする。"""

    _validate_input_binding(manifest, loaded.value)
    _validate_evidence_artifacts(
        manifest,
        loaded,
        generator_evidence_path,
    )
    if isinstance(loaded.value, LockObservationInput):
        return observe_locks(
            loaded,
            database,
            output_directory,
            clock=clock,
            sleeper=sleeper,
        )

    query_results, executions = _execute_snapshot_queries(
        database,
        loaded.value.mode,
        event_id=loaded.value.request.event_id,
        round_id=loaded.value.request.round_id,
        schema_revision=loaded.value.request.schema_revision,
    )
    sealed_at = aware_now(clock)
    if isinstance(loaded.value, PerRoundVerificationInput):
        report = build_per_round_report(
            manifest,
            loaded,
            query_results,
            executions,
            sealed_at,
        )
    else:
        report = build_final_report(
            manifest,
            cast(FinalVerificationInput, loaded.value),
            loaded.artifact,
            query_results,
            executions,
            sealed_at,
        )
    return seal_bound_report(report, output_directory)


def _execute_snapshot_queries(
    database: VerifierDatabase,
    mode: Literal["per-round", "final"],
    *,
    event_id: UUID,
    round_id: UUID,
    schema_revision: str,
) -> tuple[dict[str, tuple[StrictDbVerifierModel, ...]], tuple[QueryExecutionMetadata, ...]]:
    results: dict[str, tuple[StrictDbVerifierModel, ...]] = {}
    executions: list[QueryExecutionMetadata] = []
    with database.read_only_snapshot(expected_schema_revision=schema_revision) as snapshot:
        for spec in query_specs_for_mode(mode):
            available = {"event_id": event_id, "round_id": round_id}
            parameters = {name: available[name] for name in spec.bind_names}
            query_rows = cast(
                tuple[StrictDbVerifierModel, ...],
                snapshot.execute_query(spec, parameters),
            )
            results[spec.query_id] = query_rows
            executions.append(
                QueryExecutionMetadata(query_id=spec.query_id, row_count=len(query_rows))
            )
    return results, tuple(executions)


def _validate_evidence_artifacts(
    manifest: LoadTestManifest,
    loaded: LoadedDbVerifierRequest,
    generator_path: Path | None,
) -> None:
    """evidence本文をstrict再読込し、requestが固定したidentityと一致させる。"""

    if isinstance(loaded.value, LockObservationInput):
        if generator_path is not None:
            raise DbVerifierError("db_verifier_evidence_not_allowed")
        return
    if generator_path is None:
        raise DbVerifierError("db_verifier_evidence_required")

    request = loaded.value.request
    try:
        generator = load_generator_evidence(
            generator_path,
            manifest,
            round_number=request.round_number,
            round_id=request.round_id,
        )
    except VerificationHandoffError:
        raise DbVerifierError("db_verifier_evidence_invalid") from None
    if generator.artifact != request.generator_evidence:
        raise DbVerifierError("db_verifier_evidence_identity_mismatch")


def _validate_input_binding(manifest: LoadTestManifest, value: DbVerifierRequest) -> None:
    context = manifest_context(manifest)
    if isinstance(value, LockObservationInput):
        actual = (
            value.run_id,
            value.event_id,
            value.source_commit,
            value.schema_revision,
            value.manifest_sha256,
            value.scenario_sha256,
        )
        expected = (
            context.run_id,
            context.event_id,
            context.source_commit,
            context.schema_revision,
            context.manifest_sha256,
            context.scenario_sha256,
        )
        scenario = manifest.scenario.rounds[value.round_number - 1]
        open_at = manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)
        if (
            actual != expected
            or value.observation_start_at != open_at
            or value.observation_end_at
            != open_at + timedelta(milliseconds=LOCK_OBSERVATION_DURATION_MS)
        ):
            raise DbVerifierError("db_verifier_input_binding_mismatch")
        return

    request = value.request
    scenario = manifest.scenario.rounds[request.round_number - 1]
    question = manifest.questions[request.round_number - 1]
    status_count = 50 if scenario.condition == "receipt-discard" else 0
    replay_count = scenario.virtual_answer_count if scenario.replay == "all" else 0
    expected_oracle = (
        500,
        600,
        scenario.virtual_answer_count,
        (
            scenario.expected_option_counts.a,
            scenario.expected_option_counts.b,
            scenario.expected_option_counts.c,
            scenario.expected_option_counts.d,
        ),
        manifest.scenario.thresholds.round_start_width_max_ms,
        0,
        0,
        0,
        0,
        replay_count,
        replay_count,
        0,
        status_count,
        status_count,
        status_count,
    )
    actual_oracle = (
        request.oracle.total_answer_count,
        request.oracle.virtual_session_count,
        request.oracle.virtual_answer_count,
        request.oracle.option_counts.as_tuple(),
        request.oracle.round_start_width_max_ms,
        request.oracle.unresolved_answer_maximum,
        request.oracle.missed_round_maximum,
        request.oracle.duplicate_answer_maximum,
        request.oracle.misleading_success_maximum,
        request.oracle.replay_attempt_count,
        request.oracle.replay_same_answer_id_count,
        request.oracle.replay_row_change_maximum,
        request.oracle.receipt_discard_count,
        request.oracle.answer_status_query_count,
        request.oracle.status_recovered_count,
    )
    round_start = manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)
    round_end = (
        manifest.scheduled_start_at
        + timedelta(seconds=manifest.scenario.rounds[request.round_number].start_offset_seconds)
        if request.round_number < 10
        else manifest.scheduled_start_at
        + timedelta(seconds=manifest.scenario.lifecycle.measurement_end_seconds)
    )
    actual_binding = (
        request.run_id,
        request.event_id,
        request.source_commit,
        request.schema_revision,
        request.manifest_sha256,
        request.scenario_sha256,
        request.question_id,
        request.scenario_condition,
    )
    expected_binding = (
        context.run_id,
        context.event_id,
        context.source_commit,
        context.schema_revision,
        context.manifest_sha256,
        context.scenario_sha256,
        question.id,
        scenario.condition,
    )
    if (
        actual_binding != expected_binding
        or actual_oracle != expected_oracle
        or request.verification_id
        != verification_id(
            manifest.run_id,
            request.round_number,
            request.verification_requested_head_sha256,
        )
        or (request.round_number == 1 and request.round_id != manifest.initial_round.id)
        or not round_start <= request.requested_at < round_end
    ):
        raise DbVerifierError("db_verifier_input_binding_mismatch")
