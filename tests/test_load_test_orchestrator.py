from __future__ import annotations

import signal
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from tests.test_load_test_manifest import REPOSITORY_ROOT, _manifest

from quiz_app import load_test_orchestrator as entry
from quiz_app.load_test import orchestrator as module
from quiz_app.load_test import orchestrator_process
from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.db_verifier_artifacts import write_canonical_artifact
from quiz_app.load_test.orchestrator import LoadTestOrchestrator, RampReady
from quiz_app.load_test.orchestrator_contracts import (
    OrchestratorError,
    RunFiles,
    RunnerConfiguration,
    cleanup_input,
    load_runner_configuration,
)
from quiz_app.load_test.orchestrator_process import RunClock, RunInterrupted
from quiz_app.load_test.verification_contracts import ArtifactIdentity
from quiz_app.load_test_aws.models import AwsPreparationEvidence, SchemaReceipt


@pytest.fixture(autouse=True)
def isolated_collection_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    # 回収の期限・完全照合・ACK異常はcollection専用回帰で検証する。
    monkeypatch.setattr(entry, "publish_and_wait", MagicMock())
    monkeypatch.setattr(
        entry, "collection_deadline", lambda *args: datetime.now(UTC) + timedelta(seconds=60)
    )


def _files(tmp_path: Path) -> RunFiles:
    root = (tmp_path / str(_manifest().run_id)).resolve()
    root.mkdir(mode=0o700)
    (root / "inputs").mkdir(mode=0o700)
    return RunFiles(root, REPOSITORY_ROOT)


def _prepared() -> AwsPreparationEvidence:
    now = datetime.now(UTC)
    manifest = _manifest(public_origin="https://10.0.1.20")
    return AwsPreparationEvidence(
        run_id=manifest.run_id,
        account_id="123456789012",
        source_commit="a" * 40,
        manifest_sha256=manifest_context(manifest).manifest_sha256,
        app_instance_id="i-1234567890abcdef0",
        runner_instance_id="i-1234567890abcdef1",
        app_private_ip="10.0.1.20",
        runner_private_ip="10.0.1.21",
        public_origin=manifest.public_origin,
        ca_sha256="a" * 64,
        prepared_at=now,
        expires_at=now + timedelta(hours=3),
    )


class AdvancingClock(RunClock):
    def __init__(self, now: datetime) -> None:
        super().__init__(now + timedelta(hours=3))
        self.value = now

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        self.check()


def _bare_runner(files: RunFiles) -> LoadTestOrchestrator:
    runner = LoadTestOrchestrator.__new__(LoadTestOrchestrator)
    runner.files = files
    runner.manifest = _manifest()
    runner.prepared = _prepared()
    runner.aws = MagicMock()
    runner.clock = AdvancingClock(runner.manifest.scheduled_start_at - timedelta(seconds=60))
    runner.generator = MagicMock()
    runner.fault_monitor = None
    runner.artifacts = []
    runner.completed_rounds = 0
    runner.http = MagicMock()
    return runner


def test_ramp_requires_matching_600_websocket_evidence_and_server_count(tmp_path: Path) -> None:
    runner = _bare_runner(_files(tmp_path))
    runner.clock.value = runner.manifest.scheduled_start_at + timedelta(seconds=600)
    assert not runner._ramp_ready()
    evidence = RampReady(
        format="load-test-generator-ramp-ready-v1",
        run_id=runner.manifest.run_id,
        event_id=runner.manifest.event.id,
        manifest_sha256=manifest_context(runner.manifest).manifest_sha256,
        sessions=600,
        websocket_connected=600,
        observed_at_unix_ms=int(runner.manifest.scheduled_start_at.timestamp() * 1000) + 600_000,
    )
    write_canonical_artifact(
        runner.files.output("generator"), "generator-ramp-ready.json", evidence
    )
    runner.http.get_event_state.return_value = SimpleNamespace(participant_count=599)
    with pytest.raises(OrchestratorError, match="ramp_evidence_invalid"):
        runner._ramp_ready()
    runner.http.get_event_state.return_value = SimpleNamespace(participant_count=600)
    assert runner._ramp_ready()
    assert len(runner.artifacts) == 1
    runner.clock.value += timedelta(seconds=2.001)
    with pytest.raises(OrchestratorError, match="ramp_evidence_invalid"):
        runner._ramp_ready()


@pytest.mark.parametrize(
    "change", [{"sessions": 598}, {"websocket_connected": 599}, {"ready": 600}]
)
def test_retired_or_incomplete_ramp_contract_refused(change: dict[str, int]) -> None:
    payload = {
        "format": "load-test-generator-ramp-ready-v1",
        "run_id": str(_manifest().run_id),
        "event_id": str(_manifest().event.id),
        "manifest_sha256": "a" * 64,
        "sessions": 600,
        "websocket_connected": 600,
        "observed_at_unix_ms": 1,
        **change,
    }
    import json

    with pytest.raises(ValidationError):
        RampReady.model_validate_json(json.dumps(payload))


def test_config_rejects_arbitrary_command_and_old_real_device_fields() -> None:
    for field in ("command", "real_devices"):
        with pytest.raises(ValidationError):
            RunnerConfiguration.model_validate(
                {
                    "format": "load-test-orchestrator-v1",
                    "run_id": _manifest().run_id,
                    "postgres_ca_sha256": "a" * 64,
                    field: "untrusted",
                }
            )


def test_wait_is_bounded_and_propagates_generator_failure(tmp_path: Path) -> None:
    runner = _bare_runner(_files(tmp_path))
    with pytest.raises(OrchestratorError, match="phase_deadline"):
        runner._wait_for(lambda: False, 1)
    runner.generator.check.side_effect = OrchestratorError("child_failed")
    with pytest.raises(OrchestratorError, match="child_failed"):
        runner._wait_until(600)


def test_signal_and_deadline_stop_without_waiting() -> None:
    clock = RunClock(datetime.now(UTC) + timedelta(seconds=60))
    clock.signal(signal.SIGTERM, None)
    with pytest.raises(RunInterrupted):
        clock.sleep(60)
    clock = RunClock(datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(OrchestratorError, match="deadline"):
        clock.check()


def test_generator_uses_explicit_node_ca_without_ambient_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _files(tmp_path)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-propagate")
    spawn = MagicMock()
    monkeypatch.setattr(orchestrator_process.subprocess, "Popen", spawn)
    generator = orchestrator_process.GeneratorProcess(files)
    generator.start()
    environment = spawn.call_args.kwargs["env"]
    assert environment == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(files.root),
        "LANG": "C",
        "TZ": "UTC",
        "NODE_EXTRA_CA_CERTS": str(files.input("application-ca.pem")),
    }
    assert spawn.call_args.kwargs["start_new_session"] is True


def test_fault_monitor_failure_aborts_before_next_mutation(tmp_path: Path) -> None:
    runner = _bare_runner(_files(tmp_path))
    failed: Future[bool] = Future()
    failed.set_exception(OrchestratorError("fault_failed"))
    runner.fault_monitor = failed
    with pytest.raises(OrchestratorError, match="fault_failed"):
        runner._check()


@pytest.mark.parametrize(
    "outcome,code", [(None, 0), (RuntimeError("private-error"), 1), (RunInterrupted("stop"), 130)]
)
def test_entry_requests_same_cleanup_on_success_failure_and_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: BaseException | None,
    code: int,
) -> None:
    files = _files(tmp_path)
    prepared = _prepared()
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    monkeypatch.setattr(entry, "BotoAwsApi", MagicMock())
    monkeypatch.setattr(RunFiles, "load_binding", lambda self, **kwargs: (_manifest(), prepared))
    monkeypatch.setattr(entry, "load_runner_configuration", MagicMock())
    runner = MagicMock()
    runner.run.side_effect = outcome
    runner.run.return_value.model_dump_json.return_value = '{"decision":"PASS"}'
    monkeypatch.setattr(entry, "LoadTestOrchestrator", MagicMock(return_value=runner))
    cleanup = MagicMock(return_value=SimpleNamespace(arn="arn:aws:states:execution:cleanup"))
    monkeypatch.setattr(entry, "request_cleanup", cleanup)
    monkeypatch.setattr(entry, "log_failure", MagicMock())
    assert entry.main(["--run-directory", str(files.root)]) == code
    assert cleanup.call_args.args[1] == cleanup_input(prepared)
    cleanup.assert_called_once()
    runner.generator.close.assert_called_once()
    assert "private-error" not in capsys.readouterr().err


def test_invalid_config_still_requests_cleanup_after_preparation_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _files(tmp_path)
    prepared = _prepared()
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    monkeypatch.setattr(entry, "BotoAwsApi", MagicMock())
    monkeypatch.setattr(RunFiles, "load_binding", MagicMock(side_effect=ValueError("invalid")))
    cleanup = MagicMock(return_value=SimpleNamespace(arn="arn:cleanup"))
    monkeypatch.setattr(entry, "request_cleanup", cleanup)
    monkeypatch.setattr(entry, "log_failure", MagicMock())
    assert entry.main(["--run-directory", str(files.root)]) == 1
    cleanup.assert_called_once()


def test_cleanup_request_failure_returns_nonzero_even_after_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _files(tmp_path)
    prepared = _prepared()
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    monkeypatch.setattr(entry, "BotoAwsApi", MagicMock())
    monkeypatch.setattr(RunFiles, "load_binding", lambda self, **kwargs: (_manifest(), prepared))
    monkeypatch.setattr(entry, "load_runner_configuration", MagicMock())
    runner = MagicMock()
    runner.run.return_value.model_dump_json.return_value = '{"decision":"PASS"}'
    monkeypatch.setattr(entry, "LoadTestOrchestrator", MagicMock(return_value=runner))
    monkeypatch.setattr(entry, "request_cleanup", MagicMock(side_effect=ValueError("cleanup")))
    monkeypatch.setattr(entry, "log_failure", MagicMock())
    assert entry.main(["--run-directory", str(files.root)]) == 1


def test_all_ten_rounds_wait_for_evidence_and_db_before_advancing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _bare_runner(_files(tmp_path))
    runner.preflight = MagicMock()
    runner._ramp_ready = MagicMock(return_value=True)
    identities = ArtifactIdentity(name="report.json", sha256="a" * 64, size_bytes=10)
    verified = SimpleNamespace(report=identities, receipt=identities)
    runner.database = MagicMock()
    runner.database.verify_round.return_value = verified
    runner.database.verify.return_value = verified
    runner.database.seal.return_value = identities
    runner.database.observe_locks.return_value = verified
    runner.http.get_readiness.return_value = SimpleNamespace(
        status="ready", database="ok", migration="current", redis="ok"
    )
    status = SimpleNamespace(verification_request_artifact=identities, next_action="wait-results")
    calls: list[tuple[str, float]] = []

    def action(_controller: object, name: str, **_kwargs: object) -> object:
        offset = (runner.clock.now() - runner.manifest.scheduled_start_at).total_seconds()
        if name == "advance" and runner.completed_rounds == 9:
            runner.database.seal.assert_called_once_with(status, mode="final")
            runner.database.verify.assert_not_called()
        calls.append((name, offset))
        return status

    runner._action = action
    for number in range(1, 11):
        path = runner.files.output("generator") / f"generator-round-{number:02d}.json"
        path.write_text("{}")
        path.chmod(0o600)
    summary = runner.files.output("generator") / "generator-metrics-summary.json"
    summary.write_text("{}")
    summary.chmod(0o600)
    monitor: Future[bool] = Future()
    monitor.set_result(True)
    monkeypatch.setattr(module, "start_app_worker", MagicMock(return_value="command"))
    monkeypatch.setattr(module, "monitor_app_worker", MagicMock(return_value=monitor))
    monkeypatch.setattr(module, "app_worker_status", MagicMock(return_value=True))
    monkeypatch.setattr(module, "download_archive", MagicMock(return_value=()))
    monkeypatch.setattr(module, "seal_acceptance", MagicMock(return_value=identities))
    executor = MagicMock()

    def submit(function: object, number: int) -> Future[object]:
        future: Future[object] = Future()
        future.set_result(runner.database.observe_locks(number))
        return future

    executor.__enter__.return_value.submit.side_effect = submit
    monkeypatch.setattr(module, "ThreadPoolExecutor", MagicMock(return_value=executor))
    result = runner.run()
    assert result.decision == "PASS" and result.completed_rounds == 10
    assert [name for name, _ in calls].count("prepare") == 10
    assert [name for name, _ in calls] == ["prepare"] + [
        "schedule",
        "publish",
        "wait-results",
        "advance",
        "prepare",
    ] * 9 + ["schedule", "publish", "wait-results", "advance"]
    schedules = [offset for name, offset in calls if name == "schedule"]
    assert schedules == [
        slot.start_offset_seconds - 5.5 for slot in runner.manifest.scenario.rounds
    ]
    assert runner.database.verify_round.call_count == 10
    runner.database.verify.assert_called_once_with(
        identities, generator=runner.files.output("generator") / "generator-round-10.json"
    )
    assert runner.database.observe_locks.call_count == 10
    assert (runner.clock.now() - runner.manifest.scheduled_start_at).total_seconds() >= 4800


def test_actual_external_run_inputs_bind_manifest_ca_schema_and_deadline(tmp_path: Path) -> None:
    files = _files(tmp_path)
    now = datetime.now(UTC)
    manifest = _manifest(
        created_at=now - timedelta(minutes=10),
        scheduled_start_at=now + timedelta(minutes=10),
        public_origin="https://10.0.1.20",
    )
    ca = files.input("application-ca.pem")
    ca.write_bytes(b"public-ca-fixture")
    ca.chmod(0o600)
    prepared = _prepared().model_copy(
        update={
            "manifest_sha256": manifest_context(manifest).manifest_sha256,
            "ca_sha256": sha256(ca.read_bytes()).hexdigest(),
            "prepared_at": now,
        }
    )
    write_canonical_artifact(files.root / "inputs", "manifest.json", manifest)
    write_canonical_artifact(files.root / "inputs", "preparation.json", prepared)
    config = RunnerConfiguration(
        format="load-test-orchestrator-v1", run_id=manifest.run_id, postgres_ca_sha256="b" * 64
    )
    write_canonical_artifact(files.root / "inputs", "orchestrator.json", config)
    schema = SchemaReceipt(
        run_id=manifest.run_id,
        account_id=prepared.account_id,
        schema_revision=manifest.schema_revision,
        verified_at=now - timedelta(minutes=1),
        schema_applied_by_human=True,
        application_role_is_nonowner=True,
        verifier_role_is_read_only=True,
        single_event_count=1,
    )
    write_canonical_artifact(files.root / "inputs", "schema.json", schema)
    assert files.load_binding(now=now) == (manifest, prepared)
    assert load_runner_configuration(files, prepared, manifest) == config
    ca.write_bytes(b"changed-ca")
    with pytest.raises(OrchestratorError, match="preparation_binding_invalid"):
        files.load_binding(now=now)
