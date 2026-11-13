from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid5

import pytest
from db_verifier_fixtures import (
    RUN_ID,
    _directory,
    _evidence_files,
    _FakeDatabase,
    _final_results,
    _loaded,
    _manifest,
    _open_at,
    _request,
)

from quiz_app.load_test.db_verifier import FinalVerificationReport, run_db_verifier
from quiz_app.load_test.db_verifier_contracts import FinalVerificationInput
from quiz_app.load_test.db_verifier_database import VerifierDatabase
from quiz_app.load_test.db_verifier_final import _valid_final_rounds
from quiz_app.load_test.db_verifier_queries import FinalRoundRow, ReadinessSummaryRow


def test_final_verification_recomputes_all_fixed_aggregate_counts(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 10)
    value = FinalVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="final",
        request=_request(manifest, 10, evidence),
    )
    output = _directory(tmp_path, "final")

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, _FakeDatabase(_final_results(manifest))),
        output,
        generator_evidence_path=evidence.generator_path,
        clock=lambda: _open_at(manifest, 10) + timedelta(seconds=60),
    )

    assert result.decision == "PASS"
    report = FinalVerificationReport.model_validate_json((output / result.report.name).read_bytes())
    assert report.facts.model_dump() == {
        "participant_count": 600,
        "preparation_target_count": 6_000,
        "readiness_report_count": 6_000,
        "answer_count": 5_000,
        "score_ledger_count": 5_000,
        "virtual_answer_count": 5_000,
        "option_stat_count": 40,
        "rank_snapshot_count": 10,
        "rank_snapshot_row_count": 5_016,
        "experienced_participant_count": 502,
        "nonresponder_count": 98,
        "idempotency_record_count": 40,
        "preparation_audit_count": 10,
        "schedule_audit_count": 10,
        "aggregation_audit_count": 10,
        "publish_audit_count": 10,
        "advance_audit_count": 9,
        "complete_audit_count": 1,
        "backup_validation_audit_count": 10,
    }
    assert report.round_identity_scope.startswith("round-1-and-round-10-exact")


@pytest.mark.parametrize(
    ("ready_count", "asset_failure_count", "expected_decision"),
    [(588, 11, "PASS"), (587, 11, "FAIL"), (588, 12, "FAIL")],
)
def test_final_readiness_uses_manifest_gate_boundaries(
    tmp_path: Path,
    ready_count: int,
    asset_failure_count: int,
    expected_decision: str,
) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 10)
    query_rows = _final_results(manifest)
    readiness = cast(
        tuple[ReadinessSummaryRow, ...],
        query_rows["final_readiness_summaries"],
    )
    query_rows["final_readiness_summaries"] = tuple(
        row.model_copy(
            update={
                "ready_count": ready_count,
                "asset_failure_count": asset_failure_count,
            }
        )
        for row in readiness
    )
    value = FinalVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="final",
        request=_request(manifest, 10, evidence),
    )
    output = _directory(tmp_path, "final-readiness")

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, _FakeDatabase(query_rows)),
        output,
        generator_evidence_path=evidence.generator_path,
        clock=lambda: _open_at(manifest, 10) + timedelta(seconds=60),
    )

    assert result.decision == expected_decision
    report = FinalVerificationReport.model_validate_json((output / result.report.name).read_bytes())
    assert ("final_readiness_mismatch" in report.failure_codes) == (expected_decision == "FAIL")


def test_final_rounds_require_runtime_uuid4_after_manifest_round_one() -> None:
    manifest = _manifest()
    final_rounds = cast(
        tuple[FinalRoundRow, ...],
        _final_results(manifest)["final_rounds"],
    )
    invalid = (
        final_rounds[0],
        final_rounds[1].model_copy(update={"round_id": uuid5(RUN_ID, "invalid-runtime-round")}),
        *final_rounds[2:],
    )

    assert _valid_final_rounds(manifest, final_rounds[-1].round_id, final_rounds)
    assert not _valid_final_rounds(manifest, final_rounds[-1].round_id, invalid)


def test_final_round_versions_must_be_strictly_increasing() -> None:
    manifest = _manifest()
    final_rounds = cast(
        tuple[FinalRoundRow, ...],
        _final_results(manifest)["final_rounds"],
    )
    invalid = (
        *final_rounds[:4],
        final_rounds[4].model_copy(update={"round_version": final_rounds[3].round_version}),
        *final_rounds[5:],
    )

    assert _valid_final_rounds(manifest, final_rounds[-1].round_id, final_rounds)
    assert not _valid_final_rounds(manifest, final_rounds[-1].round_id, invalid)
