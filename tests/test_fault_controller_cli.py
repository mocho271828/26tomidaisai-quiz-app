from __future__ import annotations

import io
import json
import os
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Self
from uuid import UUID

import pytest

from quiz_app import fault_controller_cli as cli
from quiz_app.load_test.controller_contracts import ControllerRefusal
from quiz_app.load_test.fault_controller_contracts import FaultControllerStatus
from quiz_app.load_test.fault_controller_host import FaultControllerHostError


class _TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def _arguments(tmp_path: Path) -> Namespace:
    repository_root = tmp_path / "repository"
    release_directory = repository_root / "release"
    manifest_directory = repository_root / "manifest"
    journal_directory = tmp_path / "external-journal"
    snapshot_directory = tmp_path / "recovery-snapshot"
    for directory in (
        repository_root,
        release_directory,
        manifest_directory,
        journal_directory,
        snapshot_directory,
    ):
        directory.mkdir(mode=0o700, exist_ok=True)
    release = release_directory / "candidate.env"
    rollback = release_directory / "rollback.env"
    manifest = manifest_directory / "manifest.json"
    for path in (release, rollback, manifest):
        path.write_text("{}")
    return Namespace(
        action="run",
        repository_root=repository_root,
        release=release,
        rollback=rollback,
        journal=journal_directory / "fault-controller.jsonl",
        recovery_snapshot_directory=snapshot_directory,
    )


def _release_artifact_environment(tmp_path: Path) -> tuple[tuple[str, str], ...]:
    artifact_root = tmp_path / "release-artifacts"
    file_root = artifact_root / "files"
    backup_directory = artifact_root / "round-backups"
    metrics_directory = artifact_root / "metrics"
    for directory in (file_root, backup_directory, metrics_directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_fields = {
        "AWS_DEMO_RUNTIME_ENV_FILE": file_root / "runtime.env",
        "AWS_DEMO_POSTGRES_CA_FILE": file_root / "postgres-ca.pem",
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE": file_root / "toxiproxy.json",
        "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE": file_root / "postgres-password",
        "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE": file_root / "redis-password",
        "AWS_DEMO_LOAD_TEST_MANIFEST_FILE": file_root / "manifest.json",
    }
    for path in file_fields.values():
        path.touch(mode=0o600)
    return tuple(
        sorted(
            {
                **{field: str(path) for field, path in file_fields.items()},
                "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY": str(backup_directory),
                "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY": str(metrics_directory),
            }.items()
        )
    )


def test_confirmation_requires_tty_and_exact_newline() -> None:
    with pytest.raises(ControllerRefusal, match="fault_confirmation_requires_tty"):
        cli._read_confirmation("EXPECTED", io.StringIO("EXPECTED\n"))
    assert cli._read_confirmation("EXPECTED", _TtyInput("EXPECTED\r\n")) == "EXPECTED"
    with pytest.raises(ControllerRefusal, match="fault_confirmation_mismatch"):
        cli._read_confirmation("EXPECTED", _TtyInput("EXPECTED"))


def test_cli_rejects_journal_overlapping_repository(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path)
    overlapping = arguments.repository_root / "fault-journal" / "journal.jsonl"
    overlapping.parent.mkdir(mode=0o700)

    with pytest.raises(cli.FaultControllerArgumentError):
        cli._require_execution_artifact_isolation(
            overlapping,
            arguments.recovery_snapshot_directory,
            repository_root=arguments.repository_root,
            protected_files=(arguments.release, arguments.rollback, arguments.release),
        )


def test_cli_rejects_ancestor_and_artifact_directory_overlap(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path)
    repository_snapshot = arguments.repository_root / "recovery-snapshot"
    repository_snapshot.mkdir(mode=0o700)
    nested_journal_directory = arguments.recovery_snapshot_directory / "journal"
    nested_journal_directory.mkdir(mode=0o700)
    protected_files = (
        arguments.release,
        arguments.rollback,
        arguments.repository_root / "manifest" / "manifest.json",
    )

    invalid_pairs = (
        (tmp_path / "fault-controller.jsonl", arguments.recovery_snapshot_directory),
        (
            arguments.journal,
            repository_snapshot,
        ),
        (
            nested_journal_directory / "fault-controller.jsonl",
            arguments.recovery_snapshot_directory,
        ),
    )
    for journal, snapshot_directory in invalid_pairs:
        with pytest.raises(cli.FaultControllerArgumentError):
            cli._require_execution_artifact_isolation(
                journal,
                snapshot_directory,
                repository_root=arguments.repository_root,
                protected_files=protected_files,
            )


@pytest.mark.parametrize("protected_name", ["round-backups", "metrics"])
def test_cli_rejects_release_artifact_directory_overlap(
    tmp_path: Path,
    protected_name: str,
) -> None:
    arguments = _arguments(tmp_path)
    protected_directory = tmp_path / "release-artifacts" / protected_name
    protected_directory.mkdir(mode=0o700, parents=True)
    nested = protected_directory / "fault-controller"
    nested.mkdir(mode=0o700)

    with pytest.raises(cli.FaultControllerArgumentError):
        cli._require_execution_artifact_isolation(
            nested / "journal.jsonl",
            arguments.recovery_snapshot_directory,
            repository_root=arguments.repository_root,
            protected_files=(arguments.release, arguments.rollback),
            protected_directories=(protected_directory,),
        )


@pytest.mark.parametrize("artifact", ["journal", "snapshot"])
def test_execute_rejects_artifact_inside_rollback_runtime_secret_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    arguments = _arguments(tmp_path)
    rollback_secret_directory = tmp_path / "rollback-secrets"
    rollback_secret_directory.mkdir(mode=0o700)
    rollback_runtime = rollback_secret_directory / "runtime.env"
    rollback_runtime.touch(mode=0o600)
    nested = rollback_secret_directory / artifact
    nested.mkdir(mode=0o700)
    if artifact == "journal":
        arguments.journal = nested / "fault-controller.jsonl"
    else:
        arguments.recovery_snapshot_directory = nested
    manifest = SimpleNamespace(run_id=UUID("11111111-1111-4111-8111-111111111111"))
    release_environment = _release_artifact_environment(tmp_path)
    rollback_environment = dict(release_environment)
    rollback_environment["AWS_DEMO_RUNTIME_ENV_FILE"] = str(rollback_runtime)
    configuration = SimpleNamespace(
        manifest=manifest,
        manifest_file=arguments.repository_root / "manifest" / "manifest.json",
        repository_root=arguments.repository_root,
        release_file=arguments.release,
        rollback_file=arguments.rollback,
        release_environment=release_environment,
        rollback_environment=tuple(sorted(rollback_environment.items())),
    )
    monkeypatch.setattr(cli, "load_fault_host_configuration", lambda *_args: configuration)
    monkeypatch.setattr(cli, "load_manifest", lambda *_args: manifest)

    with pytest.raises(cli.FaultControllerArgumentError):
        cli._execute(arguments, _TtyInput("unused\n"))


@pytest.mark.parametrize(
    ("controller_status", "failure_code", "expected_exit_code"),
    [
        ("completed", None, os.EX_OK),
        ("failed", "fault_action_failed", os.EX_SOFTWARE),
    ],
)
def test_execute_prints_only_completed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    controller_status: Literal["completed", "failed"],
    failure_code: Literal["fault_action_failed"] | None,
    expected_exit_code: int,
) -> None:
    arguments = _arguments(tmp_path)
    run_id = UUID("11111111-1111-4111-8111-111111111111")
    manifest = SimpleNamespace(run_id=run_id)
    configuration = SimpleNamespace(
        manifest=manifest,
        manifest_file=arguments.repository_root / "manifest" / "manifest.json",
        repository_root=arguments.repository_root,
        release_file=arguments.release,
        rollback_file=arguments.rollback,
        release_environment=_release_artifact_environment(tmp_path),
        rollback_environment=_release_artifact_environment(tmp_path),
    )
    identity = object()
    host = object()
    recovery_snapshot = object()
    sensitive_value = "must-not-leak-from-host"
    events: list[str] = []

    class OrderedTtyInput(_TtyInput):
        def readline(self, size: int = -1) -> str:
            events.append("confirmation")
            return super().readline(size)

    class FakeJournal:
        def __init__(self, *args: object, **kwargs: object) -> None:
            events.append("journal")
            assert args == (arguments.journal, arguments.repository_root)
            assert kwargs == {"identity": identity}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeController:
        def __init__(self, *args: object) -> None:
            assert args[0:2] == (manifest, identity)
            assert isinstance(args[2], FakeJournal)
            assert args[3] is host

        @staticmethod
        def confirmation_prompt() -> str:
            return f"CONFIRM {run_id} FAULT-SCHEDULE REDIS API-SECONDARY AUTO-RECOVERY"

        @staticmethod
        def run(confirmation: str) -> FaultControllerStatus:
            assert confirmation == FakeController.confirmation_prompt()
            return FaultControllerStatus(
                status=controller_status,
                next_slot=None,
                journal_head_sha256="a" * 64,
                failure_code=failure_code,
            )

    monkeypatch.setattr(cli, "load_fault_host_configuration", lambda *_args: configuration)
    monkeypatch.setattr(cli, "load_manifest", lambda *_args: manifest)
    monkeypatch.setattr(
        cli,
        "seal_fault_recovery_compose_snapshot",
        lambda _configuration, _directory: events.append("seal") or recovery_snapshot,
    )
    monkeypatch.setattr(
        cli,
        "FaultHostAdapter",
        lambda _configuration, _snapshot: (
            events.append("host") or host
            if _snapshot is recovery_snapshot
            else pytest.fail("unexpected snapshot")
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_fault_controller_identity",
        lambda *_args: events.append("identity") or identity,
    )
    monkeypatch.setattr(cli, "FaultControllerJournal", FakeJournal)
    monkeypatch.setattr(cli, "LoadTestFaultController", FakeController)

    result = cli._execute(
        arguments,
        OrderedTtyInput(f"{FakeController.confirmation_prompt()}\n"),
    )

    output = capsys.readouterr()
    assert result == expected_exit_code
    assert events[:5] == ["confirmation", "seal", "host", "identity", "journal"]
    assert f'"status":"{controller_status}"' in output.out
    assert sensitive_value not in output.out + output.err


def test_main_redacts_invalid_argv_detail(capsys: pytest.CaptureFixture[str]) -> None:
    sensitive_value = "argv-secret-must-not-leak"

    result = cli.main([sensitive_value])

    output = capsys.readouterr()
    assert result == os.EX_CONFIG
    assert output.err == '{"status":"refused","code":"invalid_contract"}\n'
    assert sensitive_value not in output.out + output.err


def test_main_redacts_host_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = "host-secret-must-not-leak"

    def fail(_arguments: Namespace, _input_stream: object) -> int:
        raise FaultControllerHostError(sensitive_value)

    monkeypatch.setattr(cli, "_execute", fail)
    argv = [
        "run",
        "--repository-root",
        "/repository",
        "--release",
        "/external/release.env",
        "--rollback",
        "/external/rollback.env",
        "--journal",
        "/external/journal/controller.jsonl",
        "--recovery-snapshot-directory",
        "/external/recovery-snapshot",
    ]

    result = cli.main(argv, input_stream=_TtyInput())

    output = capsys.readouterr()
    assert result == os.EX_UNAVAILABLE
    assert output.err == '{"status":"failed","code":"host_dependency_error"}\n'
    assert sensitive_value not in output.out + output.err


@pytest.mark.parametrize(
    "error_code",
    [
        "fault_host_ntp_unsynchronized",
        "fault_host_schema_revision_invalid",
        "fault_host_gateway_port_invalid",
        "fault_host_recovery_snapshot_invalid",
        "fault_host_toxiproxy_container_invalid",
    ],
)
def test_main_preserves_known_secret_free_host_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_code: str,
) -> None:
    def fail(_arguments: Namespace, _input_stream: object) -> int:
        raise FaultControllerHostError(error_code)

    monkeypatch.setattr(cli, "_execute", fail)
    argv = [
        "run",
        "--repository-root",
        "/repository",
        "--release",
        "/external/release.env",
        "--rollback",
        "/external/rollback.env",
        "--journal",
        "/external/journal/controller.jsonl",
        "--recovery-snapshot-directory",
        "/external/recovery-snapshot",
    ]

    result = cli.main(argv, input_stream=_TtyInput())

    output = capsys.readouterr()
    assert result == os.EX_UNAVAILABLE
    assert json.loads(output.err) == {
        "status": "failed",
        "code": error_code,
    }


def test_main_redacts_unknown_refusal_and_emits_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = 'secret"\n{"injected":true}'

    def refuse(_arguments: Namespace, _input_stream: object) -> int:
        raise ControllerRefusal(sensitive_value)

    monkeypatch.setattr(cli, "_execute", refuse)
    argv = [
        "run",
        "--repository-root",
        "/repository",
        "--release",
        "/external/release.env",
        "--rollback",
        "/external/rollback.env",
        "--journal",
        "/external/journal/controller.jsonl",
        "--recovery-snapshot-directory",
        "/external/recovery-snapshot",
    ]

    result = cli.main(argv, input_stream=_TtyInput())

    output = capsys.readouterr()
    assert result == os.EX_CONFIG
    assert json.loads(output.err) == {
        "status": "refused",
        "code": "fault_controller_refused",
    }
    assert sensitive_value not in output.out + output.err


def test_main_preserves_known_secret_free_refusal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error_code = "fault_controller_journal_time_invalid"

    def refuse(_arguments: Namespace, _input_stream: object) -> int:
        raise ControllerRefusal(error_code)

    monkeypatch.setattr(cli, "_execute", refuse)
    argv = [
        "run",
        "--repository-root",
        "/repository",
        "--release",
        "/external/release.env",
        "--rollback",
        "/external/rollback.env",
        "--journal",
        "/external/journal/controller.jsonl",
        "--recovery-snapshot-directory",
        "/external/recovery-snapshot",
    ]

    result = cli.main(argv, input_stream=_TtyInput())

    output = capsys.readouterr()
    assert result == os.EX_CONFIG
    assert json.loads(output.err) == {"status": "refused", "code": error_code}
