"""AWS負荷試験の固定障害scheduleを実行する秘密非保持CLI。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Never, TextIO

from pydantic import ValidationError

from quiz_app.load_test.controller_contracts import ControllerRefusal
from quiz_app.load_test.fault_controller import LoadTestFaultController
from quiz_app.load_test.fault_controller_host import (
    FaultControllerHostError,
    FaultHostAdapter,
    build_fault_controller_identity,
    load_fault_host_configuration,
)
from quiz_app.load_test.fault_controller_journal import FaultControllerJournal
from quiz_app.load_test.fault_recovery_snapshot import (
    seal_fault_recovery_compose_snapshot,
)
from quiz_app.load_test.manifest import load_manifest

_RELEASE_ARTIFACT_FILE_FIELDS = (
    "AWS_DEMO_RUNTIME_ENV_FILE",
    "AWS_DEMO_POSTGRES_CA_FILE",
    "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
    "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
)
_RELEASE_ARTIFACT_DIRECTORY_FIELDS = (
    "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY",
    "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
)

_SAFE_REFUSAL_CODES = frozenset(
    {
        "controller_fault_schedule_invalid",
        "controller_round_invalid",
        "fault_action_failed",
        "fault_clock_invalid",
        "fault_command_identity_mismatch",
        "fault_confirmation_mismatch",
        "fault_confirmation_requires_tty",
        "fault_controller_journal_chain_invalid",
        "fault_controller_journal_directory_invalid",
        "fault_controller_journal_full",
        "fault_controller_journal_identity_duplicate",
        "fault_controller_journal_identity_invalid",
        "fault_controller_journal_identity_required",
        "fault_controller_journal_invalid",
        "fault_controller_journal_locked",
        "fault_controller_journal_not_open",
        "fault_controller_journal_time_invalid",
        "fault_controller_journal_time_reversed",
        "fault_controller_journal_write_failed",
        "fault_journal_identity_mismatch",
        "fault_journal_state_invalid",
        "fault_manifest_identity_mismatch",
        "fault_recovery_deadline_missed",
        "fault_recovered_early",
        "fault_schedule_invalid",
        "fault_slot_missed",
        "fault_target_state_drift",
        "fault_timer_invalid",
        "fault_timer_target_invalid",
    }
)
_SAFE_HOST_ERROR_CODES = frozenset(
    {
        "fault_host_action_invalid",
        "fault_host_api_action_failed",
        "fault_host_api_identity_mismatch",
        "fault_host_api_recovery_pending",
        "fault_host_api_state_invalid",
        "fault_host_api_state_mismatch",
        "fault_host_api_state_unavailable",
        "fault_host_command_invalid",
        "fault_host_command_io_invalid",
        "fault_host_command_output_too_large",
        "fault_host_command_timeout",
        "fault_host_command_unavailable",
        "fault_host_configuration_mismatch",
        "fault_host_compose_unavailable",
        "fault_host_compose_version_invalid",
        "fault_host_control_network_invalid",
        "fault_host_control_network_unavailable",
        "fault_host_docker_config_invalid",
        "fault_host_docker_context_unavailable",
        "fault_host_docker_endpoint_invalid",
        "fault_host_docker_endpoint_mismatch",
        "fault_host_event_identity_invalid",
        "fault_host_execution_host_mismatch",
        "fault_host_execution_host_unavailable",
        "fault_host_engine_unavailable",
        "fault_host_engine_version_invalid",
        "fault_host_fault_schedule_invalid",
        "fault_host_gateway_port_invalid",
        "fault_host_home_invalid",
        "fault_host_linger_disabled",
        "fault_host_linger_unavailable",
        "fault_host_manifest_identity_mismatch",
        "fault_host_manifest_invalid",
        "fault_host_manifest_unavailable",
        "fault_host_ntp_unavailable",
        "fault_host_ntp_unsynchronized",
        "fault_host_redis_command_failed",
        "fault_host_redis_output_invalid",
        "fault_host_redis_state_mismatch",
        "fault_host_release_fields_invalid",
        "fault_host_release_invalid",
        "fault_host_release_pair_mismatch",
        "fault_host_release_unavailable",
        "fault_host_recovery_snapshot_directory_invalid",
        "fault_host_recovery_snapshot_invalid",
        "fault_host_recovery_snapshot_write_failed",
        "fault_host_recovery_ownership_directory_invalid",
        "fault_host_recovery_ownership_invalid",
        "fault_host_recovery_ownership_write_failed",
        "fault_host_recovery_receipt_invalid",
        "fault_host_repository_invalid",
        "fault_host_round_scope_invalid",
        "fault_host_runtime_identity_invalid",
        "fault_host_systemd_unavailable",
        "fault_host_systemd_version_invalid",
        "fault_host_schema_revision_invalid",
        "fault_host_target_invalid",
        "fault_host_timer_arm_failed",
        "fault_host_timer_lead_invalid",
        "fault_host_timer_plan_invalid",
        "fault_host_timer_readback_failed",
        "fault_host_timer_readback_invalid",
        "fault_host_timer_readback_mismatch",
        "fault_host_toxiproxy_container_invalid",
        "fault_host_toxiproxy_container_unavailable",
        "fault_host_topology_invalid",
        "fault_host_user_manager_inactive",
        "fault_host_user_manager_unavailable",
    }
)


class FaultControllerArgumentError(ValueError):
    """argv値を保持せず、固定のCLI契約違反だけを表す。"""


class _RedactingParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise FaultControllerArgumentError


def _build_parser() -> _RedactingParser:
    parser = _RedactingParser(description="AWS負荷試験の固定4 slot fault controller")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--rollback", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--recovery-snapshot-directory", required=True, type=Path)
    return parser


def _read_confirmation(expected: str, input_stream: TextIO) -> str:
    if not input_stream.isatty():
        raise ControllerRefusal("fault_confirmation_requires_tty")
    print(f"Type exactly: {expected}", file=sys.stderr)
    supplied = input_stream.readline(512)
    if not supplied.endswith(("\n", "\r")):
        raise ControllerRefusal("fault_confirmation_mismatch")
    return supplied.rstrip("\r\n")


def _require_execution_artifact_isolation(
    journal: Path,
    recovery_snapshot_directory: Path,
    *,
    repository_root: Path,
    protected_files: tuple[Path, ...],
    protected_directories: tuple[Path, ...] = (),
) -> None:
    try:
        artifact_directories = (
            journal.parent.resolve(strict=True),
            recovery_snapshot_directory.resolve(strict=True),
        )
        protected = (
            repository_root.resolve(strict=True),
            *(path.parent.resolve(strict=True) for path in protected_files),
            *(path.resolve(strict=True) for path in protected_directories),
        )
    except OSError as error:
        raise FaultControllerArgumentError from error
    if any(
        directory == path or directory.is_relative_to(path) or path.is_relative_to(directory)
        for directory in artifact_directories
        for path in protected
    ) or (
        artifact_directories[0] == artifact_directories[1]
        or artifact_directories[0].is_relative_to(artifact_directories[1])
        or artifact_directories[1].is_relative_to(artifact_directories[0])
    ):
        raise FaultControllerArgumentError


def _execute(arguments: argparse.Namespace, input_stream: TextIO) -> int:
    configuration = load_fault_host_configuration(
        arguments.release,
        arguments.rollback,
        arguments.repository_root,
    )
    # module由来runtime rootと0600 manifest境界も既存loaderで二重照合する。
    if (
        load_manifest(configuration.manifest_file, arguments.repository_root)
        != configuration.manifest
    ):
        raise FaultControllerArgumentError
    release_environment = dict(configuration.release_environment)
    rollback_environment = dict(configuration.rollback_environment)
    release_artifact_files = tuple(
        Path(environment[field])
        for environment in (release_environment, rollback_environment)
        for field in _RELEASE_ARTIFACT_FILE_FIELDS
    )
    release_artifact_directories = tuple(
        Path(environment[field])
        for environment in (release_environment, rollback_environment)
        for field in _RELEASE_ARTIFACT_DIRECTORY_FIELDS
    )
    _require_execution_artifact_isolation(
        arguments.journal,
        arguments.recovery_snapshot_directory,
        repository_root=configuration.repository_root,
        protected_files=(
            configuration.release_file,
            configuration.rollback_file,
            *release_artifact_files,
        ),
        protected_directories=release_artifact_directories,
    )
    expected_confirmation = (
        f"CONFIRM {configuration.manifest.run_id} FAULT-SCHEDULE REDIS API-SECONDARY AUTO-RECOVERY"
    )
    confirmation = _read_confirmation(expected_confirmation, input_stream)
    recovery_snapshot = seal_fault_recovery_compose_snapshot(
        configuration,
        arguments.recovery_snapshot_directory,
    )
    host = FaultHostAdapter(configuration, recovery_snapshot)
    identity = build_fault_controller_identity(configuration, host)
    with FaultControllerJournal(
        arguments.journal,
        configuration.repository_root,
        identity=identity,
    ) as journal:
        controller = LoadTestFaultController(configuration.manifest, identity, journal, host)
        if controller.confirmation_prompt() != expected_confirmation:
            raise FaultControllerArgumentError
        status = controller.run(confirmation)
    print(status.model_dump_json())
    return os.EX_OK if status.status == "completed" else os.EX_SOFTWARE


def _run_cli(argv: list[str] | None = None, *, input_stream: TextIO | None = None) -> int:
    """成功時だけsecret-free statusを出し、例外詳細とargv値は再出力しない。"""

    try:
        arguments = _build_parser().parse_args(argv)
        return _execute(arguments, input_stream if input_stream is not None else sys.stdin)
    except ControllerRefusal as error:
        candidate = str(error)
        code = candidate if candidate in _SAFE_REFUSAL_CODES else "fault_controller_refused"
        print(
            json.dumps({"status": "refused", "code": code}, separators=(",", ":")),
            file=sys.stderr,
        )
        return os.EX_CONFIG
    except FaultControllerHostError as error:
        candidate = error.code
        code = candidate if candidate in _SAFE_HOST_ERROR_CODES else "host_dependency_error"
        print(
            json.dumps({"status": "failed", "code": code}, separators=(",", ":")),
            file=sys.stderr,
        )
        return os.EX_UNAVAILABLE
    except (FaultControllerArgumentError, ValidationError, ValueError):
        print('{"status":"refused","code":"invalid_contract"}', file=sys.stderr)
        return os.EX_CONFIG
    except (EOFError, OSError):
        print('{"status":"failed","code":"local_io_error"}', file=sys.stderr)
        return os.EX_IOERR


def main(argv: list[str] | None = None, *, input_stream: TextIO | None = None) -> int:
    """現行manifestを検証し、明示された固定障害計画を実行する。"""

    return _run_cli(argv, input_stream=input_stream)


if __name__ == "__main__":
    raise SystemExit(main())
