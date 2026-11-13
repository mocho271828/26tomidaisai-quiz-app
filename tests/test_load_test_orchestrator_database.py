from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from db_verifier_fixtures import _manifest as verification_manifest
from tests.test_evidence_handoff import _published_status
from tests.test_load_test_manifest import _manifest
from tests.test_load_test_orchestrator import AdvancingClock, _files

from quiz_app.load_test import orchestrator_database as module
from quiz_app.load_test.db_verifier_artifacts import (
    MAX_INPUT_BYTES,
    MAX_SECURE_FILE_BYTES,
    read_secure_external_file,
)
from quiz_app.load_test.db_verifier_models import DbVerifierRunResult
from quiz_app.load_test.evidence_handoff import EvidenceHandoffError
from quiz_app.load_test.orchestrator_contracts import OrchestratorError
from quiz_app.load_test.orchestrator_database import RunDatabase
from quiz_app.load_test.verification_contracts import ArtifactIdentity


@pytest.mark.parametrize("decision", ["PASS", "FAIL"])
def test_lock_observation_uses_sealed_input_and_existing_read_only_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    database = RunDatabase.__new__(RunDatabase)
    database.files = _files(tmp_path)
    database.manifest = _manifest()
    database.clock = AdvancingClock(database.manifest.scheduled_start_at)
    connection = MagicMock()
    database.connection = MagicMock(return_value=connection)
    verifier = MagicMock(
        return_value=DbVerifierRunResult(
            mode="observe-locks",
            decision=decision,
            report=ArtifactIdentity(name="locks.json", sha256="a" * 64, size_bytes=1),
            receipt=None,
        )
    )
    monkeypatch.setattr(module, "run_db_verifier", verifier)
    if decision == "FAIL":
        with pytest.raises(OrchestratorError, match="db_verification_failed"):
            database.observe_locks(3)
    else:
        database.observe_locks(3)
    loaded = verifier.call_args.args[1]
    assert loaded.value.round_number == 3
    assert loaded.value.observation_start_at == database.manifest.scheduled_start_at + timedelta(
        seconds=1620
    )
    assert loaded.value.observation_end_at - loaded.value.observation_start_at == timedelta(
        seconds=15
    )
    assert verifier.call_args.kwargs["generator_evidence_path"] is None
    assert (database.files.root / "db-inputs" / loaded.artifact.name).is_file()
    connection.dispose.assert_called_once()


@pytest.mark.parametrize("field", ["source_commit", "verification_requested_head_sha256"])
def test_automated_handoff_rejects_status_identity_or_head_drift(
    tmp_path: Path, field: str
) -> None:
    database = RunDatabase.__new__(RunDatabase)
    database.files = _files(tmp_path)
    database.manifest = verification_manifest()
    status, requests = _published_status(tmp_path, 10)
    artifact = status.verification_request_artifact
    assert artifact is not None
    path = database.files.output("requests") / artifact.name
    path.write_bytes((requests / artifact.name).read_bytes())
    path.chmod(0o600)
    changed = status.model_copy(update={field: "f" * (40 if field == "source_commit" else 64)})
    with pytest.raises(EvidenceHandoffError, match="mismatch"):
        database.seal(changed, mode="final")
    assert not tuple(database.files.output("db-inputs").iterdir())


def test_final_input_is_non_overwriting_and_rechecked_before_database_access(
    tmp_path: Path,
) -> None:
    database = RunDatabase.__new__(RunDatabase)
    database.files = _files(tmp_path)
    database.manifest = verification_manifest()
    database.connection = MagicMock()
    status, requests = _published_status(tmp_path, 10)
    artifact = status.verification_request_artifact
    assert artifact is not None
    path = database.files.output("requests") / artifact.name
    path.write_bytes((requests / artifact.name).read_bytes())
    path.chmod(0o600)
    sealed = database.seal(status, mode="final")
    with pytest.raises(EvidenceHandoffError):
        database.seal(status, mode="final")
    with pytest.raises(OrchestratorError, match="db_input_changed"):
        database.verify(sealed.model_copy(update={"sha256": "0" * 64}))
    database.connection.assert_not_called()


def test_external_read_default_limit_remains_64_mib_and_custom_limit_is_bounded(
    tmp_path: Path,
) -> None:
    files = _files(tmp_path)
    path = files.output("metrics") / "large.json"
    with path.open("wb") as output:
        output.truncate(MAX_INPUT_BYTES + 1)
    path.chmod(0o600)
    assert MAX_INPUT_BYTES == 64 * 1024 * 1024
    with pytest.raises(ValueError, match="file_invalid"):
        read_secure_external_file(path)
    path.write_bytes(b"0123")
    assert read_secure_external_file(path, maximum_bytes=4)[0] == b"0123"
    with pytest.raises(ValueError, match="file_invalid"):
        read_secure_external_file(path, maximum_bytes=3)
    for limit in (0, True, MAX_SECURE_FILE_BYTES + 1):
        with pytest.raises(ValueError, match="size_limit_invalid"):
            read_secure_external_file(path, maximum_bytes=limit)
