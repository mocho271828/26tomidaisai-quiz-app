import hashlib
import json
import os
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from pydantic import BaseModel, ValidationError

from quiz_app.load_test.controller_contracts import (
    VerificationRequestedPayload,
    canonical_json_bytes,
    manifest_context,
)
from quiz_app.load_test.controller_verification import (
    FileVerificationProvider,
    GeneratorRoundEvidence,
    LoadedGeneratorRoundEvidence,
    RoundOptionCounts,
    RoundVerificationRequest,
    VerificationHandoffError,
    build_verification_request,
    generator_identity,
    load_generator_evidence,
    load_verification_receipt,
    verification_expectation_from_request,
    write_verification_request,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest
from quiz_app.load_test.verification_contracts import (
    ArtifactIdentity,
    RoundVerificationReceipt,
)
from quiz_app.schema_revision import SCHEMA_REVISION

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
ROUND_ID = UUID("33333333-3333-4333-8333-333333333333")
CREATED_AT = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
SOURCE_COMMIT = "a" * 40
JOURNAL_HEAD = "b" * 64
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


def _round_id(manifest: LoadTestManifest, round_number: int) -> UUID:
    if round_number == 1:
        return manifest.initial_round.id
    return uuid5(manifest.run_id, f"runtime-round:{round_number}")


def _virtual_counts(_round_number: int) -> RoundOptionCounts:
    return RoundOptionCounts(a=126, b=125, c=125, d=124)


def _slot_time(manifest: LoadTestManifest, round_number: int, seconds: int) -> datetime:
    offset = manifest.scenario.rounds[round_number - 1].start_offset_seconds
    return manifest.scheduled_start_at + timedelta(seconds=offset + seconds)


def _generator_evidence(
    manifest: LoadTestManifest,
    round_number: int,
) -> GeneratorRoundEvidence:
    context = manifest_context(manifest)
    scenario = manifest.scenario.rounds[round_number - 1]
    replay_count = scenario.virtual_answer_count if scenario.replay == "all" else 0
    status_count = 50 if scenario.condition == "receipt-discard" else 0
    return GeneratorRoundEvidence(
        format="load-test-generator-round-evidence-v2",
        run_id=manifest.run_id,
        event_id=manifest.event.id,
        source_commit=manifest.artifacts.source_commit,
        schema_revision=manifest.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=round_number,
        round_id=_round_id(manifest, round_number),
        question_id=manifest.questions[round_number - 1].id,
        scenario_condition=scenario.condition,
        answer_population=scenario.answer_population,
        virtual_session_count=600,
        virtual_answer_count=scenario.virtual_answer_count,
        virtual_option_counts=_virtual_counts(round_number),
        started_answer_count=scenario.virtual_answer_count,
        resolved_answer_count=scenario.virtual_answer_count,
        request_start_width_ms=1_000,
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
        sealed_at=_slot_time(manifest, round_number, 20),
    )


def _artifact_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "handoff"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _write_json(path: Path, value: object) -> Path:
    body = (
        canonical_json_bytes(value)
        if isinstance(value, BaseModel)
        else json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
    )
    path.write_bytes(body + b"\n")
    path.chmod(0o600)
    return path


def _load_round(
    tmp_path: Path,
    manifest: LoadTestManifest,
    round_number: int,
) -> LoadedGeneratorRoundEvidence:
    directory = _artifact_directory(tmp_path)
    generator_path = _write_json(
        directory / f"generator-round-{round_number}.json",
        _generator_evidence(manifest, round_number),
    )
    return load_generator_evidence(
        generator_path,
        manifest,
        round_number=round_number,
        round_id=_round_id(manifest, round_number),
    )


def test_mixed_round_is_recomputed_from_all_500_virtual_answers(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)

    request = build_verification_request(
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 1, 30),
    )

    assert request.oracle.total_answer_count == 500
    assert request.oracle.virtual_answer_count == 500
    assert request.oracle.option_counts.as_tuple() == (126, 125, 125, 124)
    assert loaded_generator.evidence.virtual_option_counts.as_tuple() == (
        126,
        125,
        125,
        124,
    )
    assert request.generator_evidence.name == "generator-round-1.json"
    with pytest.raises(ValidationError):
        request.round_number = 2  # type: ignore[misc]


@pytest.mark.parametrize("round_number", [3, 10])
def test_replay_rounds_require_all_500_same_answer_id_replays(
    tmp_path: Path,
    round_number: int,
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, round_number)

    request = build_verification_request(
        manifest,
        round_number=round_number,
        round_id=_round_id(manifest, round_number),
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, round_number, 30),
    )

    assert request.oracle.virtual_answer_count == 500
    assert request.oracle.replay_attempt_count == 500
    assert request.oracle.replay_same_answer_id_count == 500
    assert request.oracle.replay_row_change_maximum == 0


def test_round_four_requires_all_50_discarded_receipts_to_recover_by_status(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 4)

    request = build_verification_request(
        manifest,
        round_number=4,
        round_id=_round_id(manifest, 4),
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 4, 30),
    )

    assert request.oracle.receipt_discard_count == 50
    assert request.oracle.answer_status_query_count == 50
    assert request.oracle.status_recovered_count == 50


@pytest.mark.parametrize(
    ("round_number", "mutate"),
    [
        (1, lambda payload: payload.update(virtual_session_count=598)),
        (1, lambda payload: payload.update(request_start_width_ms=1_001)),
        (1, lambda payload: payload.update(unresolved_answer_count=1)),
        (1, lambda payload: payload.update(missed_round_count=1)),
        (1, lambda payload: payload.update(duplicate_answer_count=1)),
        (1, lambda payload: payload.update(misleading_success_count=1)),
        (3, lambda payload: payload.update(replay_attempt_count=0)),
        (10, lambda payload: payload.update(replay_same_answer_id_count=499)),
        (4, lambda payload: payload.update(status_recovered_count=49)),
        (4, lambda payload: payload.update(answer_status_query_count=0)),
        (5, lambda payload: payload.update(scenario_condition="none")),
        (6, lambda payload: payload.update(event_id=str(UUID(int=4)))),
        (7, lambda payload: payload.update(question_id=str(UUID(int=5)))),
    ],
)
def test_generator_self_report_cannot_override_manifest_scenario(
    tmp_path: Path,
    round_number: int,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    manifest = _manifest()
    directory = _artifact_directory(tmp_path)
    payload = json.loads(_generator_evidence(manifest, round_number).model_dump_json())
    mutate(payload)
    path = _write_json(directory / "generator.json", payload)

    with pytest.raises(VerificationHandoffError, match="generator_evidence_mismatch"):
        load_generator_evidence(
            path,
            manifest,
            round_number=round_number,
            round_id=_round_id(manifest, round_number),
        )


def test_generator_evidence_rejects_extra_secret_and_raw_identifier_fields(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    directory = _artifact_directory(tmp_path)
    for forbidden_field in ("cookie", "csrf_token", "public_label", "client_answer_id"):
        payload = json.loads(_generator_evidence(manifest, 1).model_dump_json())
        payload[forbidden_field] = "must-not-be-stored"
        path = _write_json(directory / f"forbidden-{forbidden_field}.json", payload)
        with pytest.raises(
            VerificationHandoffError, match="generator_evidence_invalid"
        ) as captured:
            load_generator_evidence(
                path,
                manifest,
                round_number=1,
                round_id=manifest.initial_round.id,
            )
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert "must-not-be-stored" not in "".join(
            traceback.format_exception(captured.value)
        )


def test_generator_evidence_requires_canonical_json_with_one_line_feed(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    directory = _artifact_directory(tmp_path)
    path = directory / "noncanonical-generator.json"
    path.write_bytes(
        _generator_evidence(manifest, 1).model_dump_json(indent=2).encode() + b"\n"
    )
    path.chmod(0o600)

    with pytest.raises(VerificationHandoffError, match="generator_evidence_invalid"):
        load_generator_evidence(
            path,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        )


def test_external_reader_requires_0600_single_link_regular_non_symlink_file(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    directory = _artifact_directory(tmp_path)
    valid = _write_json(directory / "valid.json", _generator_evidence(manifest, 1))

    unsafe_mode = directory / "unsafe-mode.json"
    unsafe_mode.write_bytes(valid.read_bytes())
    unsafe_mode.chmod(0o640)
    with pytest.raises(VerificationHandoffError, match="handoff_artifact_file_invalid"):
        generator_identity(
            unsafe_mode,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        )

    symlink = directory / "symlink.json"
    symlink.symlink_to(valid)
    with pytest.raises(VerificationHandoffError):
        generator_identity(
            symlink,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        )

    hardlink = directory / "hardlink.json"
    os.link(valid, hardlink)
    with pytest.raises(VerificationHandoffError, match="handoff_artifact_file_invalid"):
        generator_identity(
            hardlink,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        )


def test_external_reader_rejects_repository_file() -> None:
    manifest = _manifest()
    repository_file = Path(__file__).resolve()
    with pytest.raises(
        VerificationHandoffError, match="handoff_artifact_must_be_external"
    ):
        generator_identity(
            repository_file,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        )


def test_external_reader_rejects_lexical_ancestor_symlink(tmp_path: Path) -> None:
    manifest = _manifest()
    actual_directory = tmp_path / "actual"
    actual_directory.mkdir(mode=0o700)
    actual_directory.chmod(0o700)
    path = _write_json(
        actual_directory / "generator.json", _generator_evidence(manifest, 1)
    )
    alias = tmp_path / "alias"
    alias.symlink_to(actual_directory, target_is_directory=True)

    with pytest.raises(
        VerificationHandoffError, match="handoff_artifact_directory_invalid"
    ):
        generator_identity(
            alias / path.name,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        )


def test_verification_request_is_persisted_0600_without_overwrite(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)
    request = build_verification_request(
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 1, 30),
    )
    directory = tmp_path / "handoff"

    artifact = write_verification_request(directory, request)
    request_path = directory / artifact.name

    assert stat_mode(request_path) == 0o600
    assert artifact.sha256 == hashlib.sha256(request_path.read_bytes()).hexdigest()
    assert artifact.size_bytes == request_path.stat().st_size
    assert (
        RoundVerificationRequest.model_validate_json(request_path.read_bytes())
        == request
    )
    original = request_path.read_bytes()
    assert write_verification_request(directory, request) == artifact
    assert request_path.read_bytes() == original
    changed_request = request.model_copy(
        update={"requested_at": request.requested_at + timedelta(microseconds=1)}
    )
    with pytest.raises(
        VerificationHandoffError, match="verification_request_collision"
    ):
        write_verification_request(directory, changed_request)

    alias = tmp_path / "request-directory-alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(
        VerificationHandoffError, match="handoff_artifact_directory_invalid"
    ):
        write_verification_request(alias, request)


def test_verification_request_cannot_predate_sealed_evidence(tmp_path: Path) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)

    with pytest.raises(
        VerificationHandoffError, match="verification_request_time_invalid"
    ):
        build_verification_request(
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
            verification_requested_head_sha256=JOURNAL_HEAD,
            generator=loaded_generator,
            requested_at=_slot_time(manifest, 1, 19),
        )


def test_provider_identities_fit_controller_verification_payload(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    directory = _artifact_directory(tmp_path)
    generator_path = _write_json(
        directory / "generator.json", _generator_evidence(manifest, 1)
    )
    provider = FileVerificationProvider()

    payload = VerificationRequestedPayload(
        kind="verification_requested",
        round_number=1,
        round_id=manifest.initial_round.id,
        question_id=manifest.questions[0].id,
        generator_evidence=provider.generator_identity(
            generator_path,
            manifest,
            round_number=1,
            round_id=manifest.initial_round.id,
        ),
    )

    assert payload.generator_evidence.name == "generator.json"


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_receipt_is_pass_only_and_bound_to_request_and_verified_report(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)
    request = build_verification_request(
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 1, 30),
    )
    expectation = verification_expectation_from_request(request)
    directory = tmp_path / "handoff"
    report_path = directory / "db-report.json"
    report_path.write_bytes(b'{"format":"read-only-db-report-v1","decision":"PASS"}\n')
    report_path.chmod(0o600)
    report = ArtifactIdentity(
        name=report_path.name,
        sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        size_bytes=report_path.stat().st_size,
    )
    receipt = RoundVerificationReceipt(
        format="load-test-round-verification-receipt-v2",
        verification_id=request.verification_id,
        run_id=request.run_id,
        event_id=request.event_id,
        source_commit=request.source_commit,
        schema_revision=SCHEMA_REVISION,
        manifest_sha256=request.manifest_sha256,
        scenario_sha256=request.scenario_sha256,
        round_number=request.round_number,
        round_id=request.round_id,
        question_id=request.question_id,
        verification_requested_head_sha256=request.verification_requested_head_sha256,
        generator_evidence=request.generator_evidence,
        report=report,
        decision="PASS",
        failure_codes=(),
        sealed_at=CREATED_AT + timedelta(minutes=3),
    )
    receipt_path = _write_json(directory / "receipt.json", receipt)

    loaded = load_verification_receipt(receipt_path, expectation)

    assert loaded.receipt == receipt
    assert loaded.artifact.name == "receipt.json"
    assert (
        loaded.artifact.sha256 == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    )
    provider_loaded = FileVerificationProvider().load_receipt(receipt_path, expectation)
    assert provider_loaded.receipt == receipt
    assert provider_loaded.artifact == loaded.artifact


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verification_requested_head_sha256", "c" * 64),
        ("manifest_sha256", "d" * 64),
        ("round_id", str(UUID(int=8))),
        ("question_id", str(UUID(int=9))),
        (
            "generator_evidence",
            {"name": "other.json", "sha256": "e" * 64, "size_bytes": 1},
        ),
    ],
)
def test_receipt_rejects_every_changed_binding(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)
    request = build_verification_request(
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 1, 30),
    )
    directory = tmp_path / "handoff"
    report_path = directory / "report.json"
    report_path.write_bytes(b"{}\n")
    report_path.chmod(0o600)
    report = ArtifactIdentity(
        name=report_path.name,
        sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        size_bytes=report_path.stat().st_size,
    )
    payload = {
        "format": "load-test-round-verification-receipt-v2",
        "verification_id": str(request.verification_id),
        "run_id": str(request.run_id),
        "event_id": str(request.event_id),
        "source_commit": request.source_commit,
        "schema_revision": request.schema_revision,
        "manifest_sha256": request.manifest_sha256,
        "scenario_sha256": request.scenario_sha256,
        "round_number": request.round_number,
        "round_id": str(request.round_id),
        "question_id": str(request.question_id),
        "verification_requested_head_sha256": request.verification_requested_head_sha256,
        "generator_evidence": request.generator_evidence.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "decision": "PASS",
        "failure_codes": [],
        "sealed_at": (CREATED_AT + timedelta(minutes=3)).isoformat(),
    }
    payload[field] = value
    receipt_path = _write_json(directory / "receipt.json", payload)

    with pytest.raises(VerificationHandoffError, match="verification_receipt_mismatch"):
        load_verification_receipt(
            receipt_path,
            verification_expectation_from_request(request),
        )


@pytest.mark.parametrize(
    ("decision", "failure_codes"),
    [("FAIL", []), ("PASS", ["unexpected"])],
)
def test_receipt_rejects_non_pass_or_failure_codes(
    tmp_path: Path,
    decision: str,
    failure_codes: list[str],
) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)
    request = build_verification_request(
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 1, 30),
    )
    directory = tmp_path / "handoff"
    report_path = _write_json(directory / "report.json", {})
    report = ArtifactIdentity(
        name=report_path.name,
        sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        size_bytes=report_path.stat().st_size,
    )
    payload = {
        "format": "load-test-round-verification-receipt-v2",
        "verification_id": str(request.verification_id),
        "run_id": str(request.run_id),
        "event_id": str(request.event_id),
        "source_commit": request.source_commit,
        "schema_revision": request.schema_revision,
        "manifest_sha256": request.manifest_sha256,
        "scenario_sha256": request.scenario_sha256,
        "round_number": request.round_number,
        "round_id": str(request.round_id),
        "question_id": str(request.question_id),
        "verification_requested_head_sha256": request.verification_requested_head_sha256,
        "generator_evidence": request.generator_evidence.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "decision": decision,
        "failure_codes": failure_codes,
        "sealed_at": (CREATED_AT + timedelta(minutes=3)).isoformat(),
    }
    receipt_path = _write_json(directory / "receipt.json", payload)

    with pytest.raises(VerificationHandoffError, match="verification_receipt_invalid"):
        load_verification_receipt(
            receipt_path,
            verification_expectation_from_request(request),
        )


def test_receipt_rejects_tampered_report(tmp_path: Path) -> None:
    manifest = _manifest()
    loaded_generator = _load_round(tmp_path, manifest, 1)
    request = build_verification_request(
        manifest,
        round_number=1,
        round_id=manifest.initial_round.id,
        verification_requested_head_sha256=JOURNAL_HEAD,
        generator=loaded_generator,
        requested_at=_slot_time(manifest, 1, 30),
    )
    directory = tmp_path / "handoff"
    report_path = directory / "report.json"
    report_path.write_bytes(b"before\n")
    report_path.chmod(0o600)
    report = ArtifactIdentity(
        name=report_path.name,
        sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        size_bytes=report_path.stat().st_size,
    )
    receipt = RoundVerificationReceipt(
        format="load-test-round-verification-receipt-v2",
        verification_id=request.verification_id,
        run_id=request.run_id,
        event_id=request.event_id,
        source_commit=request.source_commit,
        schema_revision=request.schema_revision,
        manifest_sha256=request.manifest_sha256,
        scenario_sha256=request.scenario_sha256,
        round_number=request.round_number,
        round_id=request.round_id,
        question_id=request.question_id,
        verification_requested_head_sha256=request.verification_requested_head_sha256,
        generator_evidence=request.generator_evidence,
        report=report,
        decision="PASS",
        failure_codes=(),
        sealed_at=CREATED_AT + timedelta(minutes=3),
    )
    receipt_path = _write_json(directory / "receipt.json", receipt)
    report_path.write_bytes(b"after-tampering\n")
    report_path.chmod(0o600)

    with pytest.raises(VerificationHandoffError, match="verification_report_mismatch"):
        load_verification_receipt(
            receipt_path,
            verification_expectation_from_request(request),
        )


def test_provider_exposes_only_read_and_validate_operations() -> None:
    provider = FileVerificationProvider()
    assert callable(provider.generator_identity)
    assert callable(provider.load_receipt)
    assert not hasattr(provider, "write_receipt")
    assert not hasattr(provider, "create_fixture_receipt")


def test_retired_generator_format_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    payload = _generator_evidence(manifest, 1).model_dump(mode="json")
    payload["format"] = "load-test-generator-round-evidence-v1"
    path = _write_json(_artifact_directory(tmp_path) / "legacy.json", payload)

    with pytest.raises(VerificationHandoffError, match="generator_evidence_invalid"):
        load_generator_evidence(
            path, manifest, round_number=1, round_id=manifest.initial_round.id
        )
