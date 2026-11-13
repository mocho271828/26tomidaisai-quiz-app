from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid5

import pytest
from db_verifier_fixtures import (
    JOURNAL_HEAD,
    RUN_ID,
    _directory,
    _evidence_files,
    _FakeDatabase,
    _loaded,
    _manifest,
    _open_at,
    _request,
    _round_results,
)

from quiz_app.load_test.controller_verification import (
    verification_expectation_from_request,
)
from quiz_app.load_test.db_verifier import (
    DbVerifierError,
    PerRoundVerificationReport,
    load_db_verifier_request,
    run_db_verifier,
)
from quiz_app.load_test.db_verifier_contracts import PerRoundVerificationInput
from quiz_app.load_test.db_verifier_database import VerifierDatabase
from quiz_app.load_test.db_verifier_queries import (
    ReadinessSummaryRow,
    ScoreLedgerRow,
    ScoringPolicyRow,
)
from quiz_app.load_test.db_verifier_round import valid_scoring_policy
from quiz_app.load_test.verification_contracts import (
    ArtifactIdentity,
    RoundVerificationReceipt,
)


def test_per_round_pass_seals_canonical_report_and_controller_receipt(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 1)
    request = _request(manifest, 1, evidence)
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1", mode="per-round", request=request
    )
    database = _FakeDatabase(_round_results(manifest, 1))
    output = _directory(tmp_path, "output")

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, database),
        output,
        generator_evidence_path=evidence.generator_path,
        clock=lambda: _open_at(manifest, 1) + timedelta(seconds=30),
    )

    assert result.decision == "PASS"
    assert result.receipt is not None
    assert database.snapshot_revisions == [manifest.schema_revision]
    report_body = (output / result.report.name).read_bytes()
    report = PerRoundVerificationReport.model_validate_json(report_body)
    assert report.facts.model_dump() == {
        "preparation_target_count": 600,
        "readiness_report_count": 600,
        "ready_count": 600,
        "asset_failure_count": 0,
        "answer_count": 500,
        "virtual_answer_count": 500,
        "score_ledger_count": 500,
        "option_stat_count": 4,
        "ranking_row_count": 500,
        "operation_audit_count": 4,
        "idempotency_record_count": 3,
    }
    assert (
        report_body
        == json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    assert b"participant_id" not in report_body
    receipt = RoundVerificationReceipt.model_validate_json(
        (output / cast(ArtifactIdentity, result.receipt).name).read_bytes()
    )
    assert receipt.report == result.report
    assert receipt.generator_evidence == request.generator_evidence
    assert receipt.verification_requested_head_sha256 == JOURNAL_HEAD
    assert verification_expectation_from_request(request).round_id == receipt.round_id
    assert (output / result.report.name).stat().st_mode & 0o777 == 0o600


def test_legacy_formal_manifest_does_not_certify_client_timing_policy() -> None:
    manifest = _manifest()
    row = cast(ScoringPolicyRow, _round_results(manifest, 1)["scoring_policy"][0])
    assert valid_scoring_policy(manifest, row)
    client_row = row.model_copy(
        update={
            "timing_basis": "client_monotonic_v1",
            "arrival_grace_ms": 2000,
            "clock_tolerance_ms": 250,
            "client_ahead_tolerance_ms": 250,
        }
    )
    assert not valid_scoring_policy(manifest, client_row)


def test_legacy_formal_manifest_rejects_nonzero_client_ahead_tolerance() -> None:
    manifest = _manifest()
    row = cast(ScoringPolicyRow, _round_results(manifest, 1)["scoring_policy"][0])
    assert not valid_scoring_policy(
        manifest, row.model_copy(update={"client_ahead_tolerance_ms": 2500})
    )


def test_round_two_uuid5_writes_fail_report_without_receipt(tmp_path: Path) -> None:
    manifest = _manifest()
    invalid_round_id = uuid5(RUN_ID, "invalid-runtime-round-2")
    evidence = _evidence_files(
        tmp_path,
        manifest,
        2,
        round_id_override=invalid_round_id,
    )
    query_rows = _round_results(manifest, 2)
    for query_id in (
        "round_identity",
        "readiness_summary",
        "answers",
        "score_ledgers",
        "option_stats",
        "operation_audits",
    ):
        query_rows[query_id] = tuple(
            row.model_copy(update={"round_id": invalid_round_id}) for row in query_rows[query_id]
        )
    query_rows["ranking_consistency"] = tuple(
        row.model_copy(update={"snapshot_round_id": invalid_round_id})
        for row in query_rows["ranking_consistency"]
    )
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 2, evidence),
    )
    output = _directory(tmp_path, "round-2-uuid5")

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, _FakeDatabase(query_rows)),
        output,
        generator_evidence_path=evidence.generator_path,
        clock=lambda: _open_at(manifest, 2) + timedelta(seconds=30),
    )

    assert result.decision == "FAIL"
    assert result.receipt is None
    report = PerRoundVerificationReport.model_validate_json(
        (output / result.report.name).read_bytes()
    )
    assert "round_identity_mismatch" in report.failure_codes


def test_score_tamper_writes_fail_report_without_receipt(tmp_path: Path) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 1)
    rows = _round_results(manifest, 1)
    ledger = cast(ScoreLedgerRow, rows["score_ledgers"][0])
    rows["score_ledgers"] = (
        ledger.model_copy(update={"total_points": ledger.total_points + 1}),
        *rows["score_ledgers"][1:],
    )
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 1, evidence),
    )

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, _FakeDatabase(rows)),
        _directory(tmp_path, "failed"),
        generator_evidence_path=evidence.generator_path,
        clock=lambda: _open_at(manifest, 1) + timedelta(seconds=30),
    )

    assert result.decision == "FAIL"
    assert result.receipt is None


@pytest.mark.parametrize(
    ("ready_count", "asset_failure_count", "expected_decision"),
    [(588, 11, "PASS"), (587, 11, "FAIL"), (588, 12, "FAIL")],
)
def test_per_round_readiness_uses_manifest_gate_boundaries(
    tmp_path: Path,
    ready_count: int,
    asset_failure_count: int,
    expected_decision: str,
) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 1)
    query_rows = _round_results(manifest, 1)
    readiness = cast(ReadinessSummaryRow, query_rows["readiness_summary"][0])
    query_rows["readiness_summary"] = (
        readiness.model_copy(
            update={
                "ready_count": ready_count,
                "asset_failure_count": asset_failure_count,
            }
        ),
    )
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 1, evidence),
    )
    output = _directory(tmp_path, "readiness")

    result = run_db_verifier(
        manifest,
        _loaded(value),
        cast(VerifierDatabase, _FakeDatabase(query_rows)),
        output,
        generator_evidence_path=evidence.generator_path,
        clock=lambda: _open_at(manifest, 1) + timedelta(seconds=30),
    )

    assert result.decision == expected_decision
    report = PerRoundVerificationReport.model_validate_json(
        (output / result.report.name).read_bytes()
    )
    assert report.facts.ready_count == ready_count
    assert report.facts.asset_failure_count == asset_failure_count
    assert ("readiness_mismatch" in report.failure_codes) == (expected_decision == "FAIL")


def test_evidence_body_is_strictly_revalidated_before_database_query(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 1)
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 1, evidence),
    )
    payload = json.loads(evidence.generator_path.read_text())
    payload["request_start_width_ms"] = 1_001
    evidence.generator_path.write_text(json.dumps(payload))
    evidence.generator_path.chmod(0o600)
    database = _FakeDatabase(_round_results(manifest, 1))

    with pytest.raises(DbVerifierError, match="^db_verifier_evidence_invalid$"):
        run_db_verifier(
            manifest,
            _loaded(value),
            cast(VerifierDatabase, database),
            _directory(tmp_path, "tampered-evidence"),
            generator_evidence_path=evidence.generator_path,
        )

    assert database.snapshot_revisions == []


def test_evidence_identity_must_exactly_match_request(tmp_path: Path) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 1)
    request = _request(manifest, 1, evidence).model_copy(
        update={
            "generator_evidence": evidence.generator_artifact.model_copy(
                update={"sha256": "f" * 64}
            )
        }
    )
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=request,
    )
    database = _FakeDatabase(_round_results(manifest, 1))

    with pytest.raises(
        DbVerifierError,
        match="^db_verifier_evidence_identity_mismatch$",
    ):
        run_db_verifier(
            manifest,
            _loaded(value),
            cast(VerifierDatabase, database),
            _directory(tmp_path, "identity-mismatch"),
            generator_evidence_path=evidence.generator_path,
        )

    assert database.snapshot_revisions == []


def test_existing_report_is_never_reused_or_overwritten(tmp_path: Path) -> None:
    manifest = _manifest()
    evidence = _evidence_files(tmp_path, manifest, 1)
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 1, evidence),
    )
    output = _directory(tmp_path, "collision")
    database = cast(VerifierDatabase, _FakeDatabase(_round_results(manifest, 1)))
    run_kwargs = {
        "generator_evidence_path": evidence.generator_path,
        "clock": lambda: _open_at(manifest, 1) + timedelta(seconds=30),
    }
    run_db_verifier(manifest, _loaded(value), database, output, **run_kwargs)

    with pytest.raises(DbVerifierError, match="^db_verifier_artifact_collision$"):
        run_db_verifier(manifest, _loaded(value), database, output, **run_kwargs)


def test_load_input_rejects_extra_field_and_insecure_mode(tmp_path: Path) -> None:
    manifest = _manifest()
    directory = _directory(tmp_path, "input")
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 1),
    )
    payload = json.loads(value.model_dump_json())
    payload["unexpected"] = True
    path = directory / "request.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)

    with pytest.raises(DbVerifierError, match="^db_verifier_input_invalid$"):
        load_db_verifier_request(path)
    path.chmod(0o644)
    with pytest.raises(DbVerifierError, match="^db_verifier_input_file_invalid$"):
        load_db_verifier_request(path)


@pytest.mark.parametrize("field", ["account_id", "idempotency_key", "is_automatic_aggregation"])
def test_aggregation_audit_requires_system_actor_and_automatic_reason(field: str) -> None:
    from quiz_app.load_test.db_verifier_queries import OperationAuditRow
    from quiz_app.load_test.db_verifier_round import valid_round_audits

    manifest = _manifest()
    request = _request(manifest, 1)
    rows = _round_results(manifest, 1)
    audits = cast(tuple[OperationAuditRow, ...], rows["operation_audits"])
    altered = tuple(
        row.model_copy(update={field: False if field == "is_automatic_aggregation" else RUN_ID})
        if row.operation == "aggregate_round_results"
        else row
        for row in audits
    )
    assert valid_round_audits(manifest, 1, request.round_id, audits)
    assert not valid_round_audits(manifest, 1, request.round_id, altered)


def test_unknown_answer_cannot_replace_vu599_in_mixed_round() -> None:
    from quiz_app.load_test.db_verifier_queries import AnswerRow, RoundIdentityRow
    from quiz_app.load_test.db_verifier_round import check_round_answers

    manifest = _manifest()
    request = _request(manifest, 1)
    rows = _round_results(manifest, 1)
    answers = cast(tuple[AnswerRow, ...], rows["answers"])
    identity = cast(RoundIdentityRow, rows["round_identity"][0])
    accepted = check_round_answers(manifest, 1, request.round_id, identity, answers)
    assert accepted.is_valid and {599, 600} <= set(accepted.virtual_participants)
    modified = (*answers[:-1], answers[-1].model_copy(update={"client_answer_id": RUN_ID}))
    assert not check_round_answers(manifest, 1, request.round_id, identity, modified).is_valid


def test_idempotency_record_actor_must_match_operator_audits() -> None:
    from quiz_app.load_test.db_verifier_queries import IdempotencyRecordRow, OperationAuditRow
    from quiz_app.load_test.db_verifier_round import valid_round_idempotency

    manifest = _manifest()
    rows = _round_results(manifest, 1)
    records = cast(tuple[IdempotencyRecordRow, ...], rows["idempotency_records"])
    audits = cast(tuple[OperationAuditRow, ...], rows["operation_audits"])
    assert valid_round_idempotency(manifest, 1, records, audits)
    tampered = tuple(row.model_copy(update={"account_id": RUN_ID}) for row in records)
    assert not valid_round_idempotency(manifest, 1, tampered, audits)


@pytest.mark.parametrize("field", ["account_id", "idempotency_key"])
def test_publication_audit_requires_operator_and_idempotency(field: str) -> None:
    from quiz_app.load_test.db_verifier_queries import OperationAuditRow
    from quiz_app.load_test.db_verifier_round import valid_round_audits

    manifest = _manifest()
    request = _request(manifest, 1)
    rows = _round_results(manifest, 1)
    audits = cast(tuple[OperationAuditRow, ...], rows["operation_audits"])
    altered = tuple(
        row.model_copy(update={field: None}) if row.operation == "publish_round_results" else row
        for row in audits
    )
    assert valid_round_audits(manifest, 1, request.round_id, audits)
    assert not valid_round_audits(manifest, 1, request.round_id, altered)
