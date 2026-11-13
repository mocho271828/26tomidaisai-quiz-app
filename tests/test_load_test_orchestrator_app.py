from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests import test_fault_controller as faults
from tests.test_load_test_manifest import _manifest
from tests.test_load_test_orchestrator import AdvancingClock, _files, _prepared

from quiz_app import load_test_app_worker as entry
from quiz_app.load_test import orchestrator_app as module
from quiz_app.load_test import orchestrator_contracts
from quiz_app.load_test.db_verifier_artifacts import write_canonical_artifact
from quiz_app.load_test.fault_controller_contracts import FaultControllerStatus
from quiz_app.load_test.orchestrator_contracts import OrchestratorError, cleanup_input
from quiz_app.load_test.orchestrator_fault_evidence import validate_fault_evidence
from quiz_app.load_test.orchestrator_process import RunInterrupted


@pytest.mark.parametrize("state", ["completed", "failed"])
def test_fault_worker_reuses_existing_controller_and_requires_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    files = _files(tmp_path)
    config = SimpleNamespace(repository_root=files.repository_root, manifest=_manifest())
    monkeypatch.setattr(module, "seal_fault_recovery_compose_snapshot", MagicMock())
    monkeypatch.setattr(module, "FaultHostAdapter", MagicMock())
    monkeypatch.setattr(module, "build_fault_controller_identity", MagicMock())
    monkeypatch.setattr(module, "FaultControllerJournal", MagicMock())
    status = FaultControllerStatus(
        status=state,
        next_slot=None,
        journal_head_sha256="a" * 64,
        failure_code="fault_action_failed" if state == "failed" else None,
    )
    controller = MagicMock()
    controller.run.return_value = status
    monkeypatch.setattr(module, "LoadTestFaultController", MagicMock(return_value=controller))
    if state == "failed":
        with pytest.raises(OrchestratorError, match="fault_schedule_failed"):
            module._faults(files, config, AdvancingClock(_manifest().scheduled_start_at))
    else:
        paths = module._faults(files, config, AdvancingClock(_manifest().scheduled_start_at))
        assert paths[0].name == "fault.jsonl" and paths[1].name == "fault-status.json"
        assert FaultControllerStatus.model_validate_json(paths[1].read_bytes()) == status
    controller.run.assert_called_once_with(controller.confirmation_prompt.return_value)


@pytest.mark.parametrize("failure", [RuntimeError("private-fault"), RunInterrupted("signal")])
def test_app_worker_requests_aws_cleanup_when_fault_process_fails_or_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    files = _files(tmp_path)
    prepared = _prepared()
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    monkeypatch.setattr(entry, "BotoAwsApi", MagicMock())
    monkeypatch.setattr(entry, "run_app_worker", MagicMock(side_effect=failure))
    cleanup = MagicMock()
    monkeypatch.setattr(entry, "request_cleanup", cleanup)
    monkeypatch.setattr(entry, "log_failure", MagicMock())
    assert entry.main(["--run-directory", str(files.root), "--action", "faults"]) == 1
    cleanup.assert_called_once()
    assert cleanup.call_args.args[1] == cleanup_input(prepared)


def test_validation_error_log_preserves_traceback_without_secret_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit = MagicMock()
    monkeypatch.setattr(orchestrator_contracts.logfire, "exception", emit)
    try:
        raise ValueError("secret-credential")
    except ValueError as error:
        orchestrator_contracts.log_failure("invalid_configuration", error)
        logged = emit.call_args.kwargs["_exc_info"]
        assert str(logged[1]) == "invalid_configuration"
        assert logged[2] is error.__traceback__
    diagnostic = capsys.readouterr().err
    assert "secret-credential" not in diagnostic
    assert json.loads(diagnostic) == {
        "status": "failed",
        "code": "invalid_configuration",
        "locations": [],
    }


def test_failure_location_survives_disabled_logfire_console_without_input_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orchestrator_contracts.logfire, "exception", MagicMock())
    private_value = "private-credential-that-must-not-be-logged"
    try:
        orchestrator_contracts.RunFiles(tmp_path / private_value, tmp_path / "repository")
    except OSError as error:
        assert private_value in str(error)
        orchestrator_contracts.log_failure("app_worker_failed", error)
    diagnostic = capsys.readouterr().err
    assert private_value not in diagnostic
    assert str(Path(orchestrator_contracts.__file__).parents[1]) not in diagnostic
    payload = json.loads(diagnostic)
    assert payload["code"] == "app_worker_failed"
    assert payload["locations"]
    assert all(item["module"].endswith(".py") and item["line"] > 0 for item in payload["locations"])


@pytest.mark.parametrize("failure", [BrokenPipeError("private-path"), ValueError("closed stream")])
def test_failed_diagnostic_output_does_not_skip_aws_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: OSError | ValueError
) -> None:
    files = _files(tmp_path)
    prepared = _prepared()
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    monkeypatch.setattr(entry, "BotoAwsApi", MagicMock())
    monkeypatch.setattr(
        entry, "run_app_worker", MagicMock(side_effect=RuntimeError("private-fault"))
    )
    monkeypatch.setattr(entry.logfire, "configure", MagicMock())
    monkeypatch.setattr(orchestrator_contracts.logfire, "exception", MagicMock())
    warning = MagicMock()
    monkeypatch.setattr(orchestrator_contracts.logfire, "warning", warning)
    cleanup = MagicMock()
    monkeypatch.setattr(entry, "request_cleanup", cleanup)
    output = MagicMock()
    output.write.side_effect = failure
    monkeypatch.setattr(sys, "stderr", output)

    # 最後のCLI status出力は失敗しても、その前に撤去要求が届く。
    with pytest.raises(type(failure)):
        entry.main(["--run-directory", str(files.root), "--action", "faults"])

    cleanup.assert_called_once()
    assert cleanup.call_args.args[1] == cleanup_input(prepared)
    warning.assert_called_once_with(
        "load-test failure diagnostic unavailable", error_code="app_worker_failed"
    )


def test_transferred_fault_journal_is_reduced_with_existing_state_machine(tmp_path: Path) -> None:
    manifest = faults._manifest()
    identity = faults._identity(manifest)
    path = faults._journal_path(tmp_path)
    status, _ = faults._run(
        manifest, identity, faults.FakeHost(faults.FakeClock(faults.STARTED_AT)), path
    )
    typed = FaultControllerStatus.model_validate(status)
    validate_fault_evidence(manifest, path.read_bytes(), typed)
    lines = path.read_bytes().splitlines()
    with pytest.raises(OrchestratorError, match="chain_invalid"):
        validate_fault_evidence(manifest, b"\n".join((lines[0], *lines[2:])) + b"\n", typed)
    with pytest.raises(OrchestratorError, match="fault_status_invalid"):
        validate_fault_evidence(
            manifest, path.read_bytes(), typed.model_copy(update={"journal_head_sha256": "0" * 64})
        )
