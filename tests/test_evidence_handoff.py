from __future__ import annotations

import hashlib
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

import pytest
from db_verifier_fixtures import _directory, _manifest, _open_at, _request, _round_id
from pydantic import ValidationError

from quiz_app import evidence_handoff_cli
from quiz_app.load_test.controller_contracts import (
    ControllerStatus,
    manifest_context,
)
from quiz_app.load_test.controller_verification import (
    verification_request_bytes,
)
from quiz_app.load_test.db_verifier import load_db_verifier_request
from quiz_app.load_test.db_verifier_contracts import (
    FinalVerificationInput,
    LockObservationInput,
    PerRoundVerificationInput,
)
from quiz_app.load_test.db_verifier_models import DbVerifierError
from quiz_app.load_test.evidence_handoff import (
    DbInputMode,
    EvidenceHandoffError,
    seal_db_verifier_input,
    seal_lock_observation_input,
)
from quiz_app.load_test.verification_contracts import ArtifactIdentity


def _scheduled_status(round_number: int) -> ControllerStatus:
    manifest = _manifest()
    context = manifest_context(manifest)
    return ControllerStatus(
        run_id=context.run_id,
        event_id=context.event_id,
        source_commit=context.source_commit,
        schema_revision=context.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        status="waiting",
        next_action="wait-results",
        round_number=round_number,
        round_id=_round_id(manifest, round_number),
        journal_head_sha256="c" * 64,
    )


def _published_status(tmp_path: Path, round_number: int) -> tuple[ControllerStatus, Path]:
    manifest = _manifest()
    context = manifest_context(manifest)
    request = _request(manifest, round_number)
    directory = _directory(tmp_path, f"request-{round_number}")
    name = f"verification-request-round-{round_number:02d}-{request.verification_id}.json"
    body = verification_request_bytes(request)
    path = directory / name
    path.write_bytes(body)
    path.chmod(0o600)
    artifact = ArtifactIdentity(
        name=name,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )
    return (
        ControllerStatus(
            run_id=context.run_id,
            event_id=context.event_id,
            source_commit=context.source_commit,
            schema_revision=context.schema_revision,
            manifest_sha256=context.manifest_sha256,
            scenario_sha256=context.scenario_sha256,
            status="waiting",
            next_action="advance",
            round_number=round_number,
            round_id=request.round_id,
            journal_head_sha256="d" * 64,
            verification_requested_head_sha256=request.verification_requested_head_sha256,
            verification_request_artifact=artifact,
        ),
        directory,
    )


def test_db_inputs_are_bound_to_published_request_and_final_is_round_ten_only(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    per_round_status, round_one_requests = _published_status(tmp_path, 1)
    per_round_output = _directory(tmp_path, "per-round-output")
    per_round_artifact = seal_db_verifier_input(
        manifest,
        per_round_status,
        round_one_requests,
        per_round_output,
        mode="per-round",
    )
    assert isinstance(
        load_db_verifier_request(per_round_output / per_round_artifact.name).value,
        PerRoundVerificationInput,
    )
    with pytest.raises(EvidenceHandoffError, match="db_input_invalid"):
        seal_db_verifier_input(
            manifest,
            per_round_status,
            round_one_requests,
            _directory(tmp_path, "invalid-final-output"),
            mode="final",
        )

    final_status, round_ten_requests = _published_status(tmp_path, 10)
    round_ten_output = _directory(tmp_path, "round-ten-per-round-output")
    round_ten_artifact = seal_db_verifier_input(
        manifest,
        final_status,
        round_ten_requests,
        round_ten_output,
        mode="per-round",
    )
    assert isinstance(
        load_db_verifier_request(round_ten_output / round_ten_artifact.name).value,
        PerRoundVerificationInput,
    )
    final_output = _directory(tmp_path, "final-output")
    final_artifact = seal_db_verifier_input(
        manifest,
        final_status,
        round_ten_requests,
        final_output,
        mode="final",
    )
    assert isinstance(
        load_db_verifier_request(final_output / final_artifact.name).value,
        FinalVerificationInput,
    )


def test_db_input_rejects_tampered_request_identity(tmp_path: Path) -> None:
    manifest = _manifest()
    status, requests = _published_status(tmp_path, 1)
    request_artifact = status.verification_request_artifact
    assert request_artifact is not None
    request_path = requests / request_artifact.name
    request_path.write_bytes(request_path.read_bytes() + b" ")

    with pytest.raises(EvidenceHandoffError, match="verification_request_mismatch"):
        seal_db_verifier_input(
            manifest,
            status,
            requests,
            _directory(tmp_path, "tamper-output"),
            mode="per-round",
        )


@pytest.mark.parametrize("state", ("scheduled", "completed"))
@pytest.mark.parametrize("mode", ("per-round", "final"))
def test_db_input_rejects_status_before_publication_or_after_completion(
    tmp_path: Path, state: str, mode: DbInputMode
) -> None:
    published, requests = _published_status(tmp_path, 10)
    status = (
        _scheduled_status(10)
        if state == "scheduled"
        else published.model_copy(update={"status": "completed", "next_action": None})
    )
    output = _directory(tmp_path, "unpublished-output")

    with pytest.raises(EvidenceHandoffError, match="^evidence_handoff_round_not_published$"):
        seal_db_verifier_input(_manifest(), status, requests, output, mode=mode)

    assert list(output.iterdir()) == []


def test_lock_input_uses_manifest_round_window(tmp_path: Path) -> None:
    manifest = _manifest()
    output = _directory(tmp_path, "lock-output")
    artifact = seal_lock_observation_input(manifest, output, round_number=5)
    loaded = load_db_verifier_request(output / artifact.name)
    assert isinstance(loaded.value, LockObservationInput)
    assert loaded.value.observation_start_at == _open_at(manifest, 5)
    assert loaded.value.observation_end_at - loaded.value.observation_start_at == timedelta(
        seconds=15
    )


def test_status_request_head_and_artifact_are_a_pair() -> None:
    context = manifest_context(_manifest())
    with pytest.raises(ValidationError):
        ControllerStatus(
            run_id=context.run_id,
            event_id=context.event_id,
            source_commit=context.source_commit,
            schema_revision=context.schema_revision,
            manifest_sha256=context.manifest_sha256,
            scenario_sha256=context.scenario_sha256,
            status="waiting",
            next_action="advance",
            round_number=1,
            round_id=_round_id(_manifest(), 1),
            journal_head_sha256="c" * 64,
            verification_requested_head_sha256="d" * 64,
        )


def test_handoff_rejects_status_from_another_run_or_manifest(tmp_path: Path) -> None:
    manifest = _manifest()
    published, requests = _published_status(tmp_path, 1)
    different_manifest = published.model_copy(update={"manifest_sha256": "f" * 64})
    with pytest.raises(EvidenceHandoffError, match="controller_status_mismatch"):
        seal_db_verifier_input(
            manifest,
            different_manifest,
            requests,
            _directory(tmp_path, "foreign-status-output"),
            mode="per-round",
        )


def test_cli_rejects_output_below_repository_root(tmp_path: Path) -> None:
    repository = _directory(tmp_path, "repository")
    output = _directory(repository, "output")
    manifest_directory = _directory(tmp_path, "manifest-cli")
    with pytest.raises(evidence_handoff_cli.EvidenceHandoffArgumentError):
        evidence_handoff_cli._require_artifact_directory_isolation(
            Namespace(
                repository_root=repository,
                manifest=manifest_directory / "manifest.json",
                output_directory=output,
            )
        )


def test_valid_noncanonical_db_input_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    value = PerRoundVerificationInput(
        format="load-test-db-verifier-input-v1",
        mode="per-round",
        request=_request(manifest, 1),
    )
    directory = _directory(tmp_path, "noncanonical-input")
    path = directory / "input.json"
    path.write_bytes(value.model_dump_json(indent=2).encode("utf-8") + b"\n")
    path.chmod(0o600)

    with pytest.raises(DbVerifierError, match="db_verifier_input_invalid"):
        load_db_verifier_request(path)
