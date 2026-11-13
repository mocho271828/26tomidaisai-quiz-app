from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from quiz_app.load_test.db_verifier_artifacts import (
    read_secure_external_file,
    write_canonical_artifact,
)
from quiz_app.load_test.fault_controller import LoadTestFaultController
from quiz_app.load_test.fault_controller_host import (
    FaultHostAdapter,
    build_fault_controller_identity,
)
from quiz_app.load_test.fault_controller_journal import FaultControllerJournal
from quiz_app.load_test.fault_host_commands import FIXED_PATH, FaultHostCommands
from quiz_app.load_test.fault_host_config import (
    FaultHostConfiguration,
    load_fault_host_configuration,
)
from quiz_app.load_test.fault_host_executor import SubprocessCommandExecutor
from quiz_app.load_test.fault_recovery_snapshot import (
    COMPOSE_PROJECT,
    seal_fault_recovery_compose_snapshot,
)
from quiz_app.load_test.metrics_export import MetricsExportArtifact
from quiz_app.load_test.orchestrator_contracts import (
    MAX_METRICS_ARTIFACT_BYTES,
    AppWorkerConfiguration,
    OrchestratorError,
    RunFiles,
    require_private_directory,
)
from quiz_app.load_test.orchestrator_process import RunClock
from quiz_app.load_test.orchestrator_remote import AppAction, upload_archive
from quiz_app.load_test_aws.aws import AwsApi


def _faults(files: RunFiles, config: FaultHostConfiguration, clock: RunClock) -> tuple[Path, ...]:
    snapshot = seal_fault_recovery_compose_snapshot(config, files.output("fault-recovery"))
    host = FaultHostAdapter(config, snapshot)
    identity = build_fault_controller_identity(config, host)
    journal_path = files.output("fault-journal") / "fault.jsonl"
    with FaultControllerJournal(journal_path, config.repository_root, identity=identity) as journal:
        controller = LoadTestFaultController(config.manifest, identity, journal, host, clock=clock)
        status = controller.run(controller.confirmation_prompt())
    if status.status != "completed":
        raise OrchestratorError("orchestrator_fault_schedule_failed")
    directory = files.output("fault-results")
    result = write_canonical_artifact(directory, "fault-status.json", status)
    return (journal_path, directory / result.name)


def _export(files: RunFiles, config: FaultHostConfiguration, clock: RunClock) -> tuple[Path, ...]:
    if clock.now() < config.manifest.scheduled_start_at + timedelta(seconds=4800):
        raise OrchestratorError("orchestrator_metrics_export_too_early")
    environment = dict(config.release_environment)
    directory = Path(environment["AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY"])
    require_private_directory(directory, files.repository_root)
    # metrics network内の既存export serviceを利用し、Prometheusをhostへ公開しない。
    argv = FaultHostCommands.docker_runtime_prefix() + (
        "compose",
        "--env-file",
        str(config.release_file),
        "--file",
        str(config.repository_root / "deploy/aws-demo/compose.yaml"),
        "--file",
        str(config.repository_root / "deploy/aws-demo/compose.load-test.yaml"),
        "--project-name",
        COMPOSE_PROJECT,
        "--profile",
        "metrics-export",
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "--no-tty",
        "metrics-export",
    )
    result = SubprocessCommandExecutor().run(
        argv,
        timeout_seconds=240,
        environment={
            "HOME": str(config.home_directory),
            "PATH": FIXED_PATH,
            "LANG": "C",
            "TZ": "UTC",
        },
    )
    if result.returncode != 0:
        raise OrchestratorError("orchestrator_metrics_export_failed")
    basename = f"metrics-{config.manifest.run_id}"
    json_path = directory / f"{basename}.json"
    raw, _ = read_secure_external_file(json_path, maximum_bytes=MAX_METRICS_ARTIFACT_BYTES)
    artifact = MetricsExportArtifact.model_validate_json(raw)
    if (
        artifact.run_id != config.manifest.run_id
        or artifact.source_commit != config.manifest.artifacts.source_commit
    ):
        raise OrchestratorError("orchestrator_metrics_export_binding_invalid")
    return (json_path, directory / f"{basename}.csv", directory / f"{basename}.sha256")


def run_app_worker(files: RunFiles, action: AppAction, aws: AwsApi, clock: RunClock) -> None:
    manifest, prepared = files.load_binding(now=clock.now())
    raw, _ = read_secure_external_file(files.input("app-worker.json"))
    app = AppWorkerConfiguration.model_validate_json(raw)
    if app.run_id != manifest.run_id:
        raise OrchestratorError("orchestrator_app_configuration_mismatch")
    configuration = load_fault_host_configuration(
        app.release_file, app.rollback_file, files.repository_root
    )
    if configuration.manifest != manifest:
        raise OrchestratorError("orchestrator_app_manifest_mismatch")
    paths = (
        _faults(files, configuration, clock)
        if action == "faults"
        else _export(files, configuration, clock)
    )
    upload_archive(aws, prepared, action, paths)
