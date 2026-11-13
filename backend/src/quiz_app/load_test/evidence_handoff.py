from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from quiz_app.load_test.controller_contracts import (
    ControllerStatus,
    VerificationRequestExpectation,
    canonical_json_bytes,
    manifest_context,
    verification_id,
)
from quiz_app.load_test.controller_verification import (
    RoundVerificationRequest,
    VerificationHandoffError,
    validate_verification_request,
    verification_expectation_from_request,
    verification_request_bytes,
)
from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    write_canonical_artifact,
)
from quiz_app.load_test.db_verifier_contracts import (
    LOCK_OBSERVATION_DURATION_MS,
    FinalVerificationInput,
    LockObservationInput,
    PerRoundVerificationInput,
)
from quiz_app.load_test.db_verifier_models import DbVerifierError
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.scenario import LoadTestRoundScenarioManifest
from quiz_app.load_test.verification_contracts import ArtifactIdentity

DbInputMode = Literal["per-round", "final"]


class EvidenceHandoffError(ValueError):
    """秘密値や path を含めない固定 code で handoff を拒否する。"""


def load_controller_status(path: Path) -> ControllerStatus:
    """controller CLI の canonical status を安全な外部 file から読む。"""

    try:
        body, _ = read_secure_external_file(path)
        status = ControllerStatus.model_validate_json(body)
    except (DbVerifierError, ValidationError):
        raise EvidenceHandoffError("evidence_handoff_controller_status_invalid") from None
    if body != canonical_json_bytes(status) + b"\n":
        raise EvidenceHandoffError("evidence_handoff_controller_status_invalid")
    return status


def seal_db_verifier_input(
    manifest: LoadTestManifest,
    status: ControllerStatus,
    verification_request_directory: Path,
    output_directory: Path,
    *,
    mode: DbInputMode,
) -> ArtifactIdentity:
    """publish 済み controller request を再検証して DB verifier 入力へ包む。"""

    request = load_bound_verification_request(
        verification_request_directory,
        status,
        manifest,
    )
    try:
        if mode == "per-round":
            value = PerRoundVerificationInput(
                format="load-test-db-verifier-input-v1",
                mode="per-round",
                request=request,
            )
        elif mode == "final":
            value = FinalVerificationInput(
                format="load-test-db-verifier-input-v1",
                mode="final",
                request=request,
            )
        else:
            raise EvidenceHandoffError("evidence_handoff_db_input_invalid")
    except ValidationError:
        raise EvidenceHandoffError("evidence_handoff_db_input_invalid") from None
    name = f"db-verifier-{mode}-round-{request.round_number:02d}-{request.verification_id}.json"
    return _write(output_directory, name, value)


def load_bound_verification_request(
    directory: Path,
    status: ControllerStatus,
    manifest: LoadTestManifest,
) -> RoundVerificationRequest:
    """status の identity と manifest の oracle に一致する sealed request だけを返す。"""

    _require_status_manifest_binding(manifest, status)
    round_number, round_id, head, artifact = _published_round(status)
    try:
        body, actual_artifact = read_secure_external_file(directory / artifact.name)
        request = RoundVerificationRequest.model_validate_json(body)
    except (DbVerifierError, ValidationError):
        raise EvidenceHandoffError("evidence_handoff_verification_request_invalid") from None
    context = manifest_context(manifest)
    scenario = _round_scenario(manifest, round_number)
    window_start, window_end = _round_window(manifest, scenario)
    expected_binding = (
        context.run_id,
        context.event_id,
        context.source_commit,
        context.schema_revision,
        context.manifest_sha256,
        context.scenario_sha256,
        round_number,
        round_id,
        manifest.questions[round_number - 1].id,
        scenario.condition,
        head,
    )
    actual_binding = (
        request.run_id,
        request.event_id,
        request.source_commit,
        request.schema_revision,
        request.manifest_sha256,
        request.scenario_sha256,
        request.round_number,
        request.round_id,
        request.question_id,
        request.scenario_condition,
        request.verification_requested_head_sha256,
    )
    if (
        actual_artifact != artifact
        or body != verification_request_bytes(request)
        or actual_binding != expected_binding
        or request.verification_id != verification_id(manifest.run_id, round_number, head)
        or not window_start <= request.requested_at < window_end
    ):
        raise EvidenceHandoffError("evidence_handoff_verification_request_mismatch")

    try:
        expectation = VerificationRequestExpectation(
            **verification_expectation_from_request(request).model_dump(),
            requested_at=request.requested_at,
        )
        validate_verification_request(directory, artifact, manifest, expectation)
    except (ValidationError, VerificationHandoffError):
        raise EvidenceHandoffError("evidence_handoff_verification_request_mismatch") from None
    return request


def seal_lock_observation_input(
    manifest: LoadTestManifest,
    output_directory: Path,
    *,
    round_number: int,
) -> ArtifactIdentity:
    """manifest の Round open から15秒間の SELECT-only lock観測入力を seal する。"""

    scenario = _round_scenario(manifest, round_number)
    context = manifest_context(manifest)
    start = manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)
    value = LockObservationInput(
        format="load-test-db-lock-observation-input-v1",
        mode="observe-locks",
        run_id=context.run_id,
        event_id=context.event_id,
        source_commit=context.source_commit,
        schema_revision=context.schema_revision,
        manifest_sha256=context.manifest_sha256,
        scenario_sha256=context.scenario_sha256,
        round_number=round_number,
        observation_start_at=start,
        observation_end_at=start + timedelta(milliseconds=LOCK_OBSERVATION_DURATION_MS),
    )
    name = f"db-lock-observation-round-{round_number:02d}-{manifest.run_id}.json"
    return _write(output_directory, name, value)


def _require_status_manifest_binding(
    manifest: LoadTestManifest,
    status: ControllerStatus,
) -> None:
    context = manifest_context(manifest)
    if (
        status.run_id,
        status.event_id,
        status.source_commit,
        status.schema_revision,
        status.manifest_sha256,
        status.scenario_sha256,
    ) != (
        context.run_id,
        context.event_id,
        context.source_commit,
        context.schema_revision,
        context.manifest_sha256,
        context.scenario_sha256,
    ):
        raise EvidenceHandoffError("evidence_handoff_controller_status_mismatch")


def _published_round(
    status: ControllerStatus,
) -> tuple[int, UUID, str, ArtifactIdentity]:
    if (
        status.status != "waiting"
        or status.next_action != "advance"
        or status.round_number is None
        or status.round_id is None
        or status.verification_requested_head_sha256 is None
        or status.verification_request_artifact is None
    ):
        raise EvidenceHandoffError("evidence_handoff_round_not_published")
    return (
        status.round_number,
        status.round_id,
        status.verification_requested_head_sha256,
        status.verification_request_artifact,
    )


def _round_scenario(
    manifest: LoadTestManifest,
    round_number: int,
) -> LoadTestRoundScenarioManifest:
    if not 1 <= round_number <= len(manifest.scenario.rounds):
        raise EvidenceHandoffError("evidence_handoff_round_invalid")
    scenario = manifest.scenario.rounds[round_number - 1]
    if scenario.number != round_number:
        raise EvidenceHandoffError("evidence_handoff_round_invalid")
    return scenario


def _round_window(
    manifest: LoadTestManifest,
    scenario: LoadTestRoundScenarioManifest,
) -> tuple[datetime, datetime]:
    start = manifest.scheduled_start_at + timedelta(seconds=scenario.start_offset_seconds)
    end_offset = (
        manifest.scenario.rounds[scenario.number].start_offset_seconds
        if scenario.number < len(manifest.scenario.rounds)
        else manifest.scenario.lifecycle.measurement_end_seconds
    )
    return start, manifest.scheduled_start_at + timedelta(seconds=end_offset)


def _write(
    directory: Path,
    name: str,
    value: PerRoundVerificationInput | FinalVerificationInput | LockObservationInput,
) -> ArtifactIdentity:
    try:
        return write_canonical_artifact(directory, name, value)
    except DbVerifierError as error:
        code = str(error) if error.args else "db_verifier_artifact_write_failed"
        raise EvidenceHandoffError(f"evidence_handoff_{code}") from None
