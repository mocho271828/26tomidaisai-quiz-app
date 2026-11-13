from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import logfire

from quiz_app.load_test.db_verifier_artifacts import write_canonical_artifact
from quiz_app.load_test.db_verifier_contracts import (
    LockObservationInput,
    QueryExecutionMetadata,
)
from quiz_app.load_test.db_verifier_database import VerifierDatabase
from quiz_app.load_test.db_verifier_models import (
    Clock,
    DbVerifierError,
    DbVerifierRunResult,
    LoadedDbVerifierRequest,
    LockObservationFacts,
    LockObservationReport,
    Sleeper,
)
from quiz_app.load_test.db_verifier_queries import LockObservationRow, query_specs_for_mode

LOCK_SAMPLE_INTERVAL_SECONDS = 1


def observe_locks(
    loaded: LoadedDbVerifierRequest,
    database: VerifierDatabase,
    output_directory: Path,
    *,
    clock: Clock,
    sleeper: Sleeper,
) -> DbVerifierRunResult:
    """1秒ごとにfresh read-only transactionでlock集計だけを取得する。"""

    value = cast(LockObservationInput, loaded.value)
    initial_now = aware_now(clock)
    if initial_now > value.observation_start_at:
        raise DbVerifierError("lock_observation_start_missed")
    duration_seconds = (value.observation_end_at - value.observation_start_at).total_seconds()
    if not duration_seconds.is_integer() or duration_seconds < 1:
        raise DbVerifierError("lock_observation_window_invalid")

    specs = query_specs_for_mode("observe-locks")
    if len(specs) != 1 or specs[0].query_id != "lock_observation":
        raise DbVerifierError("lock_observation_query_contract_invalid")
    samples: list[LockObservationRow] = []
    # 固定scenarioの観測終端sampleを含め、終了時wait=0を独立に確認する。
    for offset in range(int(duration_seconds) + 1):
        target = value.observation_start_at + timedelta(seconds=offset)
        now = aware_now(clock)
        if now < target:
            sleeper((target - now).total_seconds())
            now = aware_now(clock)
        if now < target or now >= target + timedelta(seconds=1):
            raise DbVerifierError("lock_observation_interval_missed")
        with database.read_only_snapshot(
            expected_schema_revision=value.schema_revision
        ) as snapshot:
            raw_rows = snapshot.execute_query(specs[0], {})
        if len(raw_rows) != 1 or not isinstance(raw_rows[0], LockObservationRow):
            raise DbVerifierError("lock_observation_result_invalid")
        sample = raw_rows[0]
        if not target <= sample.observed_at < target + timedelta(seconds=1):
            raise DbVerifierError("lock_observation_interval_missed")
        if samples and sample.observed_at <= samples[-1].observed_at:
            raise DbVerifierError("lock_observation_clock_invalid")
        samples.append(sample)

    final = samples[-1]
    failure_codes = (
        ()
        if final.waiting_lock_count == 0 and final.waiting_session_count == 0
        else ("final_lock_wait_nonzero",)
    )
    report = LockObservationReport(
        format="load-test-db-lock-observation-report-v1",
        mode="observe-locks",
        run_id=value.run_id,
        event_id=value.event_id,
        source_commit=value.source_commit,
        schema_revision=value.schema_revision,
        manifest_sha256=value.manifest_sha256,
        scenario_sha256=value.scenario_sha256,
        round_number=value.round_number,
        input_artifact=loaded.artifact,
        query_executions=(
            QueryExecutionMetadata(query_id="lock_observation", row_count=len(samples)),
        ),
        facts=LockObservationFacts(
            sample_interval_seconds=LOCK_SAMPLE_INTERVAL_SECONDS,
            sample_count=len(samples),
            first_observed_at=samples[0].observed_at,
            last_observed_at=final.observed_at,
            peak_total_lock_count=max(row.total_lock_count for row in samples),
            peak_waiting_lock_count=max(row.waiting_lock_count for row in samples),
            peak_waiting_session_count=max(row.waiting_session_count for row in samples),
            peak_active_transaction_count=max(row.active_transaction_count for row in samples),
            peak_idle_in_transaction_count=max(row.idle_in_transaction_count for row in samples),
            peak_oldest_transaction_age_ms=max(row.oldest_transaction_age_ms for row in samples),
            final_waiting_lock_count=final.waiting_lock_count,
            final_waiting_session_count=final.waiting_session_count,
            peak_interpretation="calibration-only",
        ),
        decision="PASS" if not failure_codes else "FAIL",
        failure_codes=failure_codes,
        sealed_at=aware_now(clock),
    )
    name = f"db-lock-observation-round-{value.round_number:02d}-{value.run_id}.json"
    artifact = write_canonical_artifact(output_directory, name, report)
    logfire.info(
        "load-test DB lock observation sealed",
        round_number=value.round_number,
        decision=report.decision,
        sample_count=len(samples),
    )
    return DbVerifierRunResult(
        mode="observe-locks",
        decision=report.decision,
        report=artifact,
        receipt=None,
    )


def aware_now(clock: Clock) -> datetime:
    """naive clock値を拒否し、呼出側の時間帯を保持する。"""

    current = clock()
    if current.tzinfo is None or current.utcoffset() is None:
        raise DbVerifierError("db_verifier_clock_invalid")
    return current
