from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.db_verifier import (
    DbVerifierError,
    LoadedDbVerifierRequest,
    LockObservationReport,
    run_db_verifier,
)
from quiz_app.load_test.db_verifier_contracts import LockObservationInput
from quiz_app.load_test.db_verifier_database import VerifierDatabase
from quiz_app.load_test.db_verifier_queries import DbQuerySpec, LockObservationRow
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest
from quiz_app.load_test.verification_contracts import ArtifactIdentity

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_AT = datetime(2026, 9, 3, tzinfo=UTC)
SOURCE_COMMIT = "a" * 40
IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"


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


def _observation_input(manifest: LoadTestManifest) -> LockObservationInput:
    context = manifest_context(manifest)
    open_at = manifest.scheduled_start_at + timedelta(
        seconds=manifest.scenario.rounds[0].start_offset_seconds
    )
    return LockObservationInput(
        format="load-test-db-lock-observation-input-v1",
        mode="observe-locks",
        run_id=RUN_ID,
        event_id=EVENT_ID,
        source_commit=SOURCE_COMMIT,
        schema_revision=manifest.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=1,
        observation_start_at=open_at,
        observation_end_at=open_at + timedelta(seconds=15),
    )


def _loaded(value: LockObservationInput) -> LoadedDbVerifierRequest:
    return LoadedDbVerifierRequest(
        value=value,
        artifact=ArtifactIdentity(
            name="lock-input.json", sha256="3" * 64, size_bytes=100
        ),
    )


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _LockSnapshot:
    def __init__(self, row: LockObservationRow) -> None:
        self.row = row

    def execute_query(
        self,
        spec: DbQuerySpec[LockObservationRow],
        parameters: Mapping[str, UUID],
    ) -> tuple[LockObservationRow, ...]:
        assert spec.query_id == "lock_observation"
        assert parameters == {}
        return (self.row,)


class _LockDatabase:
    def __init__(
        self,
        start: datetime,
        *,
        final_wait: int = 0,
        final_observation_delay_seconds: int = 0,
    ) -> None:
        self.start = start
        self.final_wait = final_wait
        self.final_observation_delay_seconds = final_observation_delay_seconds
        self.snapshot_revisions: list[str] = []

    @contextmanager
    def read_only_snapshot(
        self,
        *,
        expected_schema_revision: str,
    ) -> Iterator[_LockSnapshot]:
        index = len(self.snapshot_revisions)
        self.snapshot_revisions.append(expected_schema_revision)
        waiting = self.final_wait if index == 15 else (3 if index == 5 else 0)
        yield _LockSnapshot(
            LockObservationRow(
                observed_at=self.start
                + timedelta(
                    seconds=index
                    + (self.final_observation_delay_seconds if index == 15 else 0)
                ),
                total_lock_count=20 + index,
                waiting_lock_count=waiting,
                waiting_session_count=waiting,
                active_transaction_count=10 + index,
                idle_in_transaction_count=0,
                oldest_transaction_age_ms=Decimal(index * 100),
            )
        )


def _output(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def test_lock_observer_uses_sixteen_fresh_snapshots_and_only_final_wait_gate(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    value = _observation_input(manifest)
    clock = _Clock(value.observation_start_at)
    database = _LockDatabase(value.observation_start_at)
    output = _output(tmp_path, "pass")

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, database),
        output,
        clock=clock,
        sleeper=clock.sleep,
    )

    assert result.decision == "PASS"
    assert result.receipt is None
    assert database.snapshot_revisions == [manifest.schema_revision] * 16
    body = (output / result.report.name).read_bytes()
    report = LockObservationReport.model_validate_json(body)
    assert report.facts.sample_count == 16
    assert report.facts.peak_waiting_lock_count == 3
    assert report.facts.final_waiting_lock_count == 0
    assert report.facts.peak_interpretation == "calibration-only"
    for forbidden_key in (b'"pid"', b'"query"', b'"user"', b'"relation"'):
        assert forbidden_key not in body


def test_lock_observer_fails_only_when_final_wait_remains(tmp_path: Path) -> None:
    manifest = _manifest()
    value = _observation_input(manifest)
    clock = _Clock(value.observation_start_at)

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, _LockDatabase(value.observation_start_at, final_wait=1)),
        _output(tmp_path, "fail"),
        clock=clock,
        sleeper=clock.sleep,
    )

    assert result.decision == "FAIL"
    assert result.receipt is None


def test_lock_observer_rejects_delayed_final_database_sample(tmp_path: Path) -> None:
    manifest = _manifest()
    value = _observation_input(manifest)
    clock = _Clock(value.observation_start_at)
    database = _LockDatabase(
        value.observation_start_at,
        final_observation_delay_seconds=9,
    )

    with pytest.raises(DbVerifierError, match="^lock_observation_interval_missed$"):
        run_db_verifier(
            manifest,
            _loaded(value),
            cast(VerifierDatabase, database),
            _output(tmp_path, "delayed-final"),
            clock=clock,
            sleeper=clock.sleep,
        )

    assert len(database.snapshot_revisions) == 16


def test_lock_observer_rejects_manifest_unbound_window(tmp_path: Path) -> None:
    manifest = _manifest()
    value = _observation_input(manifest).model_copy(
        update={
            "observation_end_at": _observation_input(manifest).observation_end_at
            + timedelta(seconds=1)
        }
    )

    with pytest.raises(DbVerifierError, match="^db_verifier_input_binding_mismatch$"):
        run_db_verifier(
            manifest,
            _loaded(value),
            cast(VerifierDatabase, _LockDatabase(value.observation_start_at)),
            _output(tmp_path, "invalid"),
            clock=lambda: value.observation_start_at,
            sleeper=lambda _seconds: None,
        )
