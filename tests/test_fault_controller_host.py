from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from quiz_app.load_test import fault_recovery_ownership as ownership_module
from quiz_app.load_test.fault_controller_contracts import RecoveryTimerPlan
from quiz_app.load_test.fault_controller_host import (
    CommandEnvironment,
    CommandResult,
    FaultControllerHostError,
    FaultHostAdapter,
    FaultHostConfiguration,
    SubprocessCommandExecutor,
    build_fault_controller_identity,
    command_argv_sha256,
    load_fault_host_configuration,
)
from quiz_app.load_test.fault_docker_network import (
    DOCKER_ENGINE_VERSION_FORMAT,
    FAULT_CONTROL_GATEWAY_MODE_OPTION,
    FAULT_CONTROL_NETWORK,
    FAULT_CONTROL_NETWORK_FORMAT,
    TOXIPROXY_CONTAINER_NAME,
    parse_fault_control_network_identity,
)
from quiz_app.load_test.fault_docker_toxiproxy import (
    TOXIPROXY_COMMAND,
    TOXIPROXY_CONTAINER_FORMAT,
    parse_toxiproxy_container,
)
from quiz_app.load_test.fault_host_config import (
    FaultMutationOutcomeUncertainError,
    FaultMutationPreconditionError,
    FaultTargetRecoveryPendingError,
)
from quiz_app.load_test.fault_recovery_snapshot import (
    FaultRecoveryComposeSnapshot,
    seal_fault_recovery_compose_snapshot,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest
from quiz_app.schema_revision import SCHEMA_REVISION

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_AT = datetime(2030, 1, 1, tzinfo=UTC)
SOURCE_COMMIT = "a" * 40
API_IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
GATEWAY_IMAGE = f"registry.aws.internal/quiz/gateway@sha256:{'2' * 64}"
GENERATOR_IMAGE = f"registry.aws.internal/quiz/generator@sha256:{'3' * 64}"
ROLLBACK_API_IMAGE = f"registry.aws.internal/quiz/api@sha256:{'4' * 64}"
ROLLBACK_GATEWAY_IMAGE = f"registry.aws.internal/quiz/gateway@sha256:{'5' * 64}"
TOXIPROXY_IMAGE = f"registry/toxiproxy@sha256:{'b' * 64}"
EXECUTION_HOST = "load-host.aws.internal"
DOCKER_CONTEXT = "default"
DOCKER_CONFIG_DIRECTORY = "/etc/gs-loadtest/docker-cli"
DOCKER_COMPOSE_VERSION = b"2.39.4\n"
DOCKER_ENGINE_VERSION = b"28.0.1\n"
PROJECT = "gs-app-tomidaisai-aws-demo"
API_SERVICE = "api-secondary"
CONTAINER_ID = "b" * 64
NETWORK_ID = "d" * 64
TOXIPROXY_CONFIG_SOURCE_PLACEHOLDER = "/external/toxiproxy.json"


@dataclass(frozen=True, slots=True)
class _Call:
    argv: tuple[str, ...]
    environment: dict[str, str]
    timeout_seconds: float


class _FakeExecutor:
    def __init__(self, *responses: CommandResult) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        environment: CommandEnvironment,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(_Call(argv, dict(environment), timeout_seconds))
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv!r}")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _validated_docker_config_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """host filesystem契約は専用unit testに委ね、command境界だけを検証する。"""

    monkeypatch.setattr(
        "quiz_app.load_test.fault_host_commands.require_root_owned_docker_config_directory",
        lambda: Path(DOCKER_CONFIG_DIRECTORY),
    )


def _result(stdout: bytes = b"", *, returncode: int = 0, stderr: bytes = b"") -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _network_output(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "Attachable": False,
        "Containers": {
            CONTAINER_ID: {
                "Name": TOXIPROXY_CONTAINER_NAME,
                "EndpointID": "c" * 64,
                "MacAddress": "02:42:ac:1e:fb:02",
                "IPv4Address": "172.30.251.2/29",
                "IPv6Address": "",
            }
        },
        "Driver": "bridge",
        "EnableIPv6": False,
        "Id": NETWORK_ID,
        "Ingress": False,
        "Internal": True,
        "IPAM": {
            "Driver": "default",
            "Options": {},
            "Config": [{"Subnet": "172.30.251.0/29"}],
        },
        "Name": FAULT_CONTROL_NETWORK,
        "Options": {FAULT_CONTROL_GATEWAY_MODE_OPTION: "isolated"},
        "Scope": "local",
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _container_network(
    ip_address: str,
    prefix_length: int,
    *,
    gateway: str = "172.30.0.1",
) -> dict[str, object]:
    return {
        "IPAMConfig": None,
        "Links": None,
        "Aliases": [TOXIPROXY_CONTAINER_NAME, "toxiproxy"],
        "MacAddress": "02:42:ac:1e:00:02",
        "DriverOpts": None,
        "GwPriority": 0,
        "NetworkID": "d" * 64,
        "EndpointID": "e" * 64,
        "Gateway": gateway,
        "IPAddress": ip_address,
        "IPPrefixLen": prefix_length,
        "IPv6Gateway": "",
        "GlobalIPv6Address": "",
        "GlobalIPv6PrefixLen": 0,
        "DNSNames": [TOXIPROXY_CONTAINER_NAME, "toxiproxy"],
    }


def _networks_for_toxiproxy() -> dict[str, object]:
    return {
        f"{PROJECT}_default": _container_network("172.30.0.2", 16),
        f"{PROJECT}_fault-data": _container_network("172.30.250.2", 29, gateway=""),
        f"{PROJECT}_fault-control": _container_network("172.30.251.2", 29, gateway=""),
    }


def _toxiproxy_output(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "CapAdd": [],
        "CapDrop": ["ALL"],
        "Cmd": list(TOXIPROXY_COMMAND),
        "Entrypoint": ["/toxiproxy"],
        "ExposedPorts": {"8474/tcp": {}},
        "Health": "healthy",
        "Id": CONTAINER_ID,
        "Image": TOXIPROXY_IMAGE,
        "Name": f"/{TOXIPROXY_CONTAINER_NAME}",
        "Networks": _networks_for_toxiproxy(),
        "Mounts": [
            {
                "Type": "bind",
                "Source": TOXIPROXY_CONFIG_SOURCE_PLACEHOLDER,
                "Destination": "/run/load-test/toxiproxy.json",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            }
        ],
        "PortBindings": {},
        "Privileged": False,
        "Project": PROJECT,
        "ReadonlyRootfs": True,
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "Running": True,
        "SecurityOpt": ["no-new-privileges:true"],
        "Service": "toxiproxy",
        "User": f"{os.geteuid()}:{os.getegid()}",
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


def _manifest() -> LoadTestManifest:
    return build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2030-01-01",
        scheduled_start_at=CREATED_AT + timedelta(hours=1),
        public_origin="https://quiz.aws.internal",
        source_commit=SOURCE_COMMIT,
        api_image=API_IMAGE,
        gateway_image=GATEWAY_IMAGE,
        generator_image=GENERATOR_IMAGE,
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


def _write_secure(path: Path, body: str | bytes) -> None:
    if isinstance(body, str):
        path.write_text(body, encoding="utf-8")
    else:
        path.write_bytes(body)
    path.chmod(0o600)


def _release_values(
    external: Path,
    manifest_path: Path,
    toxiproxy_path: Path,
    toxiproxy_sha256: str,
    *,
    rollback: bool,
) -> dict[str, str]:
    return {
        "AWS_DEMO_RELEASE_ID": "release-rollback" if rollback else "release-candidate",
        "AWS_DEMO_SOURCE_COMMIT": "c" * 40 if rollback else SOURCE_COMMIT,
        "AWS_DEMO_API_IMAGE": ROLLBACK_API_IMAGE if rollback else API_IMAGE,
        "AWS_DEMO_GATEWAY_IMAGE": ROLLBACK_GATEWAY_IMAGE if rollback else GATEWAY_IMAGE,
        "AWS_DEMO_RUNTIME_ENV_FILE": str(
            external / ("rollback.runtime" if rollback else "runtime")
        ),
        "AWS_DEMO_POSTGRES_CA_FILE": str(external / "postgres-ca.pem"),
        "AWS_DEMO_POSTGRES_CA_SHA256": "6" * 64,
        "AWS_DEMO_EXECUTION_HOST": EXECUTION_HOST,
        "AWS_DEMO_DOCKER_CONTEXT": DOCKER_CONTEXT,
        "AWS_DEMO_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
        "AWS_DEMO_RUNTIME_UID": str(os.geteuid()),
        "AWS_DEMO_RUNTIME_GID": str(os.getegid()),
        "AWS_DEMO_EVENT_ID": str(EVENT_ID),
        "AWS_DEMO_ROUND_SCOPE": "load-test-ten-round",
        "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY": str(external / "round-backups"),
        "AWS_DEMO_PROMETHEUS_IMAGE": "registry/prometheus@sha256:" + "7" * 64,
        "AWS_DEMO_POSTGRES_EXPORTER_IMAGE": "registry/postgres-exporter@sha256:" + "8" * 64,
        "AWS_DEMO_REDIS_EXPORTER_IMAGE": "registry/redis-exporter@sha256:" + "9" * 64,
        "AWS_DEMO_NODE_EXPORTER_IMAGE": "registry/node-exporter@sha256:" + "a" * 64,
        "AWS_DEMO_TOXIPROXY_IMAGE": TOXIPROXY_IMAGE,
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE": str(toxiproxy_path),
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256": toxiproxy_sha256,
        "AWS_DEMO_REDIS_HOSTNAME": "redis.aws.internal",
        "AWS_DEMO_PROMETHEUS_CONFIG_SHA256": "c" * 64,
        "AWS_DEMO_POSTGRES_EXPORTER_URI": (
            "db.aws.internal:5432/postgres?sslmode=verify-full&"
            "sslrootcert=/run/secrets/aws-demo-postgres-ca.pem"
        ),
        "AWS_DEMO_POSTGRES_EXPORTER_USER": "exporter",
        "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE": str(external / "pg-exporter.secret"),
        "AWS_DEMO_REDIS_EXPORTER_ADDR": "rediss://redis.aws.internal:6380",
        "AWS_DEMO_REDIS_EXPORTER_USER": "exporter",
        "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE": str(external / "redis-exporter.secret"),
        "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY": str(external / "metrics"),
        "AWS_DEMO_LOAD_TEST_MANIFEST_FILE": str(manifest_path),
        "AWS_DEMO_SCHEMA_REVISION": SCHEMA_REVISION,
        "AWS_DEMO_GATEWAY_PORT": "8443",
    }


def _serialize_release(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _configuration_files(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, str], dict[str, str]]:
    repository = tmp_path / "repository"
    (repository / "backend").mkdir(parents=True)
    (repository / "deploy/aws-demo").mkdir(parents=True)
    (repository / "backend/pyproject.toml").touch()
    (repository / "deploy/aws-demo/compose.yaml").touch()
    (repository / "deploy/aws-demo/compose.load-test.yaml").touch()
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    external.chmod(0o700)
    manifest_path = external / "manifest.json"
    _write_secure(manifest_path, _manifest().model_dump_json())
    toxiproxy_path = external / "toxiproxy.json"
    toxiproxy_body = (
        '[{"name":"gs-app-tomidaisai-aws-demo-redis",'
        '"listen":"172.30.250.2:6380","upstream":"10.0.2.15:6380","enabled":true}]\n'
    )
    _write_secure(toxiproxy_path, toxiproxy_body)
    toxiproxy_sha256 = hashlib.sha256(toxiproxy_body.encode("ascii")).hexdigest()
    candidate = _release_values(
        external, manifest_path, toxiproxy_path, toxiproxy_sha256, rollback=False
    )
    rollback = _release_values(
        external, manifest_path, toxiproxy_path, toxiproxy_sha256, rollback=True
    )
    candidate_path = external / "candidate.env"
    rollback_path = external / "rollback.env"
    _write_secure(candidate_path, _serialize_release(candidate))
    _write_secure(rollback_path, _serialize_release(rollback))
    return repository, candidate_path, rollback_path, candidate, rollback


def _configuration(tmp_path: Path) -> FaultHostConfiguration:
    repository, candidate, rollback, _, _ = _configuration_files(tmp_path)
    configuration = load_fault_host_configuration(candidate, rollback, repository)
    home_directory = tmp_path / "home"
    home_directory.mkdir(mode=0o700)
    home_directory.chmod(0o700)
    return replace(configuration, home_directory=home_directory)


def _adapter(
    tmp_path: Path,
    *responses: CommandResult,
    clock: datetime | None = None,
) -> tuple[FaultHostAdapter, _FakeExecutor]:
    configuration = _configuration(tmp_path)
    snapshot_directory = tmp_path / "recovery-snapshot"
    snapshot_directory.mkdir(mode=0o700)
    snapshot_directory.chmod(0o700)
    recovery_snapshot = seal_fault_recovery_compose_snapshot(
        configuration,
        snapshot_directory,
    )
    configuration_source = str(configuration.toxiproxy_configuration_file).encode()
    normalized_responses = tuple(
        replace(
            response,
            stdout=response.stdout.replace(
                TOXIPROXY_CONFIG_SOURCE_PLACEHOLDER.encode(), configuration_source
            ),
        )
        for response in responses
    )
    executor = _FakeExecutor(*normalized_responses)
    now = clock if clock is not None else configuration.manifest.scheduled_start_at
    return (
        FaultHostAdapter(
            configuration,
            recovery_snapshot,
            executor,
            clock=lambda: now,
        ),
        executor,
    )


def _fault_output(operation: str, enabled: bool, *, reconciled: bool = False) -> bytes:
    return (
        json.dumps(
            {
                "enabled": enabled,
                "format": "toxiproxy-control-result-v1",
                "name": "gs-app-tomidaisai-aws-demo-redis",
                "operation": operation,
                "reconciled": reconciled,
            },
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _api_output(
    *,
    enabled: bool,
    container_id: str = CONTAINER_ID,
    extra: bool = False,
    publishers: list[dict[str, object]] | None = None,
    ports: str | None = None,
    networks: str | None = (f"{PROJECT}_default,{PROJECT}_metrics,{PROJECT}_fault-data"),
    health: str | None = None,
) -> bytes:
    body: dict[str, object] = {
        "ID": container_id,
        "Name": f"{PROJECT}-{API_SERVICE}-1",
        "Command": "uvicorn quiz_app.main:app",
        "Project": PROJECT,
        "Service": API_SERVICE,
        "State": "running" if enabled else "exited",
        "Health": ("healthy" if enabled else "") if health is None else health,
        "ExitCode": 0,
        "Publishers": publishers,
        "Image": API_IMAGE,
        "Networks": networks,
    }
    if ports is not None:
        body["Ports"] = ports
    if extra:
        body["Unexpected"] = "rejected"
    return json.dumps(body, separators=(",", ":")).encode("ascii") + b"\n"


def _recovery_plan(adapter: FaultHostAdapter, target: str) -> RecoveryTimerPlan:
    typed_target = "redis" if target == "redis" else "api-secondary"
    matching = [
        (index, action)
        for index, action in enumerate(
            adapter.configuration.manifest.scenario.fault_schedule,
            start=1,
        )
        if action.target == typed_target and action.action == "enable"
    ]
    slot, action = matching[0]
    return RecoveryTimerPlan(
        slot_number=slot,
        target=typed_target,
        trigger_at=adapter.configuration.manifest.scheduled_start_at
        + timedelta(seconds=action.offset_seconds),
        unit_name=(
            f"gs-loadtest-{adapter.configuration.manifest.run_id.hex}-{typed_target}-recover"
        ),
        command_sha256=adapter.recovery_command_sha256(typed_target),
    )


def _timer_readback(adapter: FaultHostAdapter, plan: RecoveryTimerPlan) -> tuple[bytes, bytes]:
    trigger = plan.trigger_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    description = (
        f"gs-loadtest:{adapter.configuration.manifest.run_id.hex}:"
        f"{plan.target}:{plan.command_sha256}"
    )
    ownership_directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    marker = ownership_directory / f"slot-{plan.slot_number:02d}-{plan.target}.owned"
    receipt = ownership_directory / f"slot-{plan.slot_number:02d}-{plan.target}.receipt"
    timer = {
        "Id": f"{plan.unit_name}.timer",
        "Description": description,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "waiting",
        "Conditions": "",
        "Asserts": "",
        "Unit": f"{plan.unit_name}.service",
        "TimersCalendar": f"{{ OnCalendar={trigger} ; next_elapse={trigger} }}",
        "TimersMonotonic": "",
        "OnClockChange": "no",
        "OnTimezoneChange": "no",
        "NextElapseUSecRealtime": trigger,
        "NextElapseUSecMonotonic": "0",
        "AccuracyUSec": "1us",
        "RandomizedDelayUSec": "0",
        "Persistent": "no",
        "RemainAfterElapse": "no",
        "Result": "success",
        "Transient": "yes",
    }
    service = {
        "Id": f"{plan.unit_name}.service",
        "Description": description,
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "Conditions": "",
        "Asserts": "",
        "Type": "oneshot",
        "ExecCondition": (
            "{ path=/usr/bin/test ; argv[]=/usr/bin/test -f "
            + str(marker)
            + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        ),
        "ExecStartPre": "",
        "ExecStart": (
            "{ path=/usr/bin/env ; argv[]="
            + " ".join(adapter.recovery_command(plan.target))
            + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        ),
        "ExecStartEx": (
            "{ path=/usr/bin/env ; argv[]="
            + " ".join(adapter.recovery_command(plan.target))
            + " ; flags=no-env-expand ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        ),
        "ExecStartPost": (
            "{ path=/usr/bin/mkdir ; argv[]=/usr/bin/mkdir --mode=0700 -- "
            + str(receipt)
            + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        ),
        "ExecReload": "",
        "ExecStop": "",
        "ExecStopPost": "",
        "TimeoutStartUSec": "1min 55s",
        "RuntimeMaxUSec": "1min 55s",
        "TimeoutStopUSec": "10s",
        "Restart": "no",
        "SuccessExitStatus": "",
        "RestartPreventExitStatus": "",
        "RestartForceExitStatus": "",
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "UMask": "0077",
        "NoNewPrivileges": "yes",
        "StandardInput": "null",
        "StandardOutput": "null",
        "StandardError": "null",
        "Transient": "yes",
    }

    def encode(values: dict[str, str]) -> bytes:
        return "".join(f"{key}={value}\n" for key, value in values.items()).encode()

    return encode(timer), encode(service)


def _assert_code(expected: str, operation: object) -> None:
    with pytest.raises(FaultControllerHostError) as captured:
        operation()  # type: ignore[operator]
    assert captured.value.code == expected


def test_configuration_loads_external_release_manifest_and_topology(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)

    assert configuration.manifest.run_id == RUN_ID
    assert configuration.release_identity.source_commit == SOURCE_COMMIT
    assert configuration.execution_host == EXECUTION_HOST
    assert configuration.docker_context == DOCKER_CONTEXT
    assert configuration.toxiproxy_image == TOXIPROXY_IMAGE
    assert configuration.toxiproxy_proxy.listen == "172.30.250.2:6380"
    assert configuration.toxiproxy_proxy.upstream == "10.0.2.15:6380"


def test_configuration_rejects_manifest_and_pair_identity_drift(tmp_path: Path) -> None:
    repository, candidate_path, rollback_path, candidate, rollback = _configuration_files(tmp_path)
    candidate["AWS_DEMO_SOURCE_COMMIT"] = "d" * 40
    _write_secure(candidate_path, _serialize_release(candidate))
    _assert_code(
        "fault_host_manifest_identity_mismatch",
        lambda: load_fault_host_configuration(candidate_path, rollback_path, repository),
    )

    candidate["AWS_DEMO_SOURCE_COMMIT"] = SOURCE_COMMIT
    rollback["AWS_DEMO_GATEWAY_PORT"] = "8444"
    _write_secure(candidate_path, _serialize_release(candidate))
    _write_secure(rollback_path, _serialize_release(rollback))
    _assert_code(
        "fault_host_release_pair_mismatch",
        lambda: load_fault_host_configuration(candidate_path, rollback_path, repository),
    )


def test_configuration_rejects_writable_external_release(tmp_path: Path) -> None:
    repository, candidate_path, rollback_path, _, _ = _configuration_files(tmp_path)
    candidate_path.chmod(0o666)

    _assert_code(
        "fault_host_release_invalid",
        lambda: load_fault_host_configuration(candidate_path, rollback_path, repository),
    )


def test_configuration_rejects_credentials_in_release_endpoints(tmp_path: Path) -> None:
    repository, candidate_path, rollback_path, candidate, _ = _configuration_files(tmp_path)
    candidate["AWS_DEMO_REDIS_EXPORTER_ADDR"] = (
        "rediss://exporter:must-not-enter-systemd@redis.aws.internal:6380"
    )
    _write_secure(candidate_path, _serialize_release(candidate))

    _assert_code(
        "fault_host_release_invalid",
        lambda: load_fault_host_configuration(candidate_path, rollback_path, repository),
    )


def test_configuration_rejects_nondefault_docker_context(tmp_path: Path) -> None:
    repository, candidate_path, rollback_path, candidate, _ = _configuration_files(tmp_path)
    candidate["AWS_DEMO_DOCKER_CONTEXT"] = "ambient-context"
    _write_secure(candidate_path, _serialize_release(candidate))

    _assert_code(
        "fault_host_docker_endpoint_invalid",
        lambda: load_fault_host_configuration(candidate_path, rollback_path, repository),
    )


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_code"),
    [
        ("AWS_DEMO_POSTGRES_CA_SHA256", "not-a-digest", "fault_host_release_invalid"),
        ("AWS_DEMO_PROMETHEUS_CONFIG_SHA256", "0" * 63, "fault_host_release_invalid"),
        (
            "AWS_DEMO_SCHEMA_REVISION",
            "unexpected",
            "fault_host_schema_revision_invalid",
        ),
        ("AWS_DEMO_GATEWAY_PORT", "1023", "fault_host_gateway_port_invalid"),
        ("AWS_DEMO_REDIS_EXPORTER_USER", "invalid-user", "fault_host_release_invalid"),
    ],
)
def test_configuration_rejects_invalid_release_contract_fields(
    tmp_path: Path,
    field: str,
    invalid_value: str,
    expected_code: str,
) -> None:
    repository, candidate_path, rollback_path, candidate, _ = _configuration_files(tmp_path)
    candidate[field] = invalid_value
    _write_secure(candidate_path, _serialize_release(candidate))

    _assert_code(
        expected_code,
        lambda: load_fault_host_configuration(candidate_path, rollback_path, repository),
    )


def test_preflight_checks_systemd_ntp_linger_host_and_docker_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_CANARY", "must-not-propagate")
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(b"systemd 254 (254.1)\n"),
        _result(b"254 (254.1)\n"),
        _result(b"yes\n"),
        _result(b"yes\n"),
        _result(b"active\n"),
        _result(f"{EXECUTION_HOST}\n".encode()),
        _result(b"unix:///var/run/docker.sock\n"),
    )

    observation = adapter.preflight()

    assert observation.systemd_version == 254
    assert [call.argv[0] for call in executor.calls] == [
        "/usr/bin/docker",
        "/usr/bin/docker",
        "/usr/bin/docker",
        "/usr/bin/docker",
        "/usr/bin/systemd-run",
        "/usr/bin/systemctl",
        "/usr/bin/timedatectl",
        "/usr/bin/loginctl",
        "/usr/bin/systemctl",
        "/usr/bin/hostname",
        "/usr/bin/docker",
    ]
    assert all("SECRET_CANARY" not in call.environment for call in executor.calls)
    assert executor.calls[0].argv == (
        "/usr/bin/docker",
        "--config",
        DOCKER_CONFIG_DIRECTORY,
        "--host",
        "unix:///var/run/docker.sock",
        "compose",
        "version",
        "--short",
    )
    assert executor.calls[1].argv[-3:] == (
        "version",
        "--format",
        DOCKER_ENGINE_VERSION_FORMAT,
    )
    assert executor.calls[2].argv[-4:] == (
        "inspect",
        "--format",
        FAULT_CONTROL_NETWORK_FORMAT,
        FAULT_CONTROL_NETWORK,
    )
    assert executor.calls[3].argv[-6:] == (
        "inspect",
        "--type",
        "container",
        "--format",
        TOXIPROXY_CONTAINER_FORMAT,
        TOXIPROXY_CONTAINER_NAME,
    )
    assert executor.calls[-1].argv[1:6] == (
        "--config",
        DOCKER_CONFIG_DIRECTORY,
        "--context",
        DOCKER_CONTEXT,
        "context",
    )
    assert all("AWS_DEMO_API_IMAGE" not in call.environment for call in executor.calls)


def test_preflight_rejects_unsynchronized_clock_without_continuing(
    tmp_path: Path,
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(b"systemd 254 (254.1)\n"),
        _result(b"254 (254.1)\n"),
        _result(b"no\n"),
    )

    _assert_code("fault_host_ntp_unsynchronized", adapter.preflight)
    assert len(executor.calls) == 7


@pytest.mark.parametrize(
    "manager_version",
    [
        b"253 (253.9)\n",
        b"254 (254.1)\nextra\n",
        b"systemd 254 (254.1)\n",
    ],
)
def test_preflight_rejects_stale_or_ambiguous_user_manager_version(
    tmp_path: Path,
    manager_version: bytes,
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(b"systemd 254 (254.1)\n"),
        _result(manager_version),
    )

    _assert_code("fault_host_systemd_version_invalid", adapter.preflight)
    assert len(executor.calls) == 6


def test_recovery_preflight_only_checks_host_and_docker_context(tmp_path: Path) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(f"{EXECUTION_HOST}\n".encode()),
        _result(b"unix:///var/run/docker.sock\n"),
    )

    assert adapter.recovery_preflight() is None
    assert [call.argv[0] for call in executor.calls] == [
        "/usr/bin/docker",
        "/usr/bin/docker",
        "/usr/bin/docker",
        "/usr/bin/docker",
        "/usr/bin/hostname",
        "/usr/bin/docker",
    ]
    assert executor.calls[5].argv[1:6] == (
        "--config",
        DOCKER_CONFIG_DIRECTORY,
        "--context",
        DOCKER_CONTEXT,
        "context",
    )
    assert all("XDG_RUNTIME_DIR" not in call.environment for call in executor.calls)
    assert all("AWS_DEMO_API_IMAGE" not in call.environment for call in executor.calls)


@pytest.mark.parametrize(
    "version",
    [
        b"2.19.9\n",
        b"2.29.9\n",
        b"2.39\n",
        b"2.39.4\nextra\n",
        b"v2.39.4\n",
    ],
)
def test_compose_version_must_be_supported_and_exact(
    tmp_path: Path,
    version: bytes,
) -> None:
    adapter, executor = _adapter(tmp_path, _result(version))

    _assert_code("fault_host_compose_version_invalid", adapter.bind_docker_compose_runtime)

    assert len(executor.calls) == 1
    assert executor.calls[0].argv[-3:] == ("compose", "version", "--short")


def test_preflight_rejects_compose_version_drift(tmp_path: Path) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(b"2.39.5\n"),
    )

    assert adapter.bind_docker_compose_runtime() == "2.39.4"
    _assert_code("fault_host_compose_version_invalid", adapter.preflight)

    assert len(executor.calls) == 2


def test_engine_runtime_rejects_unavailable_and_version_drift(tmp_path: Path) -> None:
    unavailable, unavailable_executor = _adapter(tmp_path / "unavailable", _result(returncode=1))
    _assert_code("fault_host_engine_unavailable", unavailable.bind_docker_engine_runtime)
    assert len(unavailable_executor.calls) == 1

    adapter, executor = _adapter(
        tmp_path / "drift",
        _result(DOCKER_ENGINE_VERSION),
        _result(DOCKER_COMPOSE_VERSION),
        _result(b"28.0.2\n"),
    )
    assert adapter.bind_docker_engine_runtime() == "28.0.1"

    _assert_code("fault_host_engine_version_invalid", adapter.preflight)
    assert len(executor.calls) == 3


def test_control_network_runtime_rejects_unavailable_and_identity_drift(
    tmp_path: Path,
) -> None:
    unavailable, unavailable_executor = _adapter(tmp_path / "unavailable", _result(returncode=1))
    _assert_code(
        "fault_host_control_network_unavailable",
        unavailable.bind_fault_control_network_runtime,
    )
    assert len(unavailable_executor.calls) == 1

    adapter, executor = _adapter(
        tmp_path / "drift",
        _result(_network_output()),
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output(Id="e" * 64)),
    )
    expected = parse_fault_control_network_identity(_network_output())
    assert adapter.bind_fault_control_network_runtime() == expected

    _assert_code("fault_host_control_network_invalid", adapter.preflight)
    assert len(executor.calls) == 4


def test_toxiproxy_runtime_rejects_unavailable_cross_inspect_race_and_drift(
    tmp_path: Path,
) -> None:
    unavailable, unavailable_executor = _adapter(
        tmp_path / "unavailable",
        _result(_network_output()),
        _result(returncode=1),
    )
    _assert_code(
        "fault_host_toxiproxy_container_unavailable",
        unavailable.bind_toxiproxy_container_runtime,
    )
    assert len(unavailable_executor.calls) == 2

    cross_race, cross_race_executor = _adapter(
        tmp_path / "cross-race",
        _result(_network_output()),
        _result(_toxiproxy_output(Id="f" * 64)),
    )
    _assert_code(
        "fault_host_toxiproxy_container_invalid",
        cross_race.bind_toxiproxy_container_runtime,
    )
    assert len(cross_race_executor.calls) == 2

    replaced_networks = _networks_for_toxiproxy()
    replaced_control = dict(replaced_networks[f"{PROJECT}_fault-control"])
    replaced_control["NetworkID"] = "f" * 64
    replaced_networks[f"{PROJECT}_fault-control"] = replaced_control
    network_race, network_race_executor = _adapter(
        tmp_path / "network-race",
        _result(_network_output()),
        _result(_toxiproxy_output(Networks=replaced_networks)),
    )
    _assert_code(
        "fault_host_toxiproxy_container_invalid",
        network_race.bind_toxiproxy_container_runtime,
    )
    assert len(network_race_executor.calls) == 2

    drift_networks = _networks_for_toxiproxy()
    drift_control = dict(drift_networks[f"{PROJECT}_fault-control"])
    drift_control["EndpointID"] = "f" * 64
    drift_networks[f"{PROJECT}_fault-control"] = drift_control
    adapter, executor = _adapter(
        tmp_path / "drift",
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output(Networks=drift_networks)),
    )
    expected = parse_toxiproxy_container(
        _toxiproxy_output(),
        expected_image=TOXIPROXY_IMAGE,
        expected_user=f"{os.geteuid()}:{os.getegid()}",
        expected_config_source=TOXIPROXY_CONFIG_SOURCE_PLACEHOLDER,
    ).identity_sha256
    assert adapter.bind_toxiproxy_container_runtime() == expected

    _assert_code("fault_host_toxiproxy_container_invalid", adapter.preflight)
    assert len(executor.calls) == 6


def test_redis_action_uses_fixed_client_and_stable_identity(tmp_path: Path) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_fault_output("probe", True)),
        _result(_fault_output("probe", True)),
        _result(_fault_output("disable", False)),
    )

    before = adapter.observe_target("redis")
    mutation = adapter.apply_action("redis", "disable", before)

    assert mutation.before.enabled is True
    assert mutation.after.enabled is False
    assert mutation.before.identity_sha256 == mutation.after.identity_sha256
    assert executor.calls[2].argv[1:6] == (
        "--config",
        DOCKER_CONFIG_DIRECTORY,
        "--host",
        "unix:///var/run/docker.sock",
        "compose",
    )
    assert "AWS_DEMO_API_IMAGE" not in executor.calls[2].environment
    assert all(call.timeout_seconds == 30 for call in executor.calls)
    assert executor.calls[2].argv[-8:] == (
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "--no-tty",
        "fault-control-client",
        "disable",
    )


def test_redis_output_must_be_exact_single_json_line(tmp_path: Path) -> None:
    adapter, _ = _adapter(tmp_path, _result(_fault_output("probe", True) + b"extra\n"))

    _assert_code("fault_host_redis_output_invalid", lambda: adapter.observe_target("redis"))


def test_redis_output_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    ambiguous = _fault_output("probe", True).replace(
        b'"enabled":true', b'"enabled":false,"enabled":true'
    )
    adapter, _ = _adapter(tmp_path, _result(ambiguous))

    _assert_code("fault_host_redis_output_invalid", lambda: adapter.observe_target("redis"))


def test_redis_mutation_rechecks_exact_expected_state(tmp_path: Path) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_fault_output("probe", True)),
        _result(_fault_output("probe", False)),
    )
    expected = adapter.observe_target("redis")

    with pytest.raises(FaultMutationPreconditionError) as captured:
        adapter.apply_action("redis", "disable", expected)

    assert captured.value.code == "fault_host_redis_state_mismatch"
    assert len(executor.calls) == 2


def test_redis_action_failure_is_outcome_uncertain(tmp_path: Path) -> None:
    adapter, _ = _adapter(
        tmp_path,
        _result(_fault_output("probe", True)),
        _result(_fault_output("probe", True)),
        _result(returncode=1),
    )
    expected = adapter.observe_target("redis")

    with pytest.raises(FaultMutationOutcomeUncertainError) as captured:
        adapter.apply_action("redis", "disable", expected)

    assert captured.value.code == "fault_host_redis_command_failed"


def test_api_stop_uses_fixed_argv_and_preserves_container_identity(
    tmp_path: Path,
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_api_output(enabled=True)),
        _result(_api_output(enabled=True)),
        _result(),
        _result(_api_output(enabled=False)),
    )

    before = adapter.observe_target("api-secondary")
    mutation = adapter.apply_action("api-secondary", "disable", before)

    assert mutation.before.enabled is True
    assert mutation.after.enabled is False
    assert mutation.before.identity_sha256 == mutation.after.identity_sha256
    assert executor.calls[2].argv[-4:] == ("stop", "--timeout", "20", "api-secondary")
    assert executor.calls[2].timeout_seconds == 115


def test_api_transition_rejects_recreated_container(tmp_path: Path) -> None:
    adapter, _ = _adapter(
        tmp_path,
        _result(_api_output(enabled=True)),
        _result(_api_output(enabled=True)),
        _result(),
        _result(_api_output(enabled=False, container_id="c" * 64)),
    )
    before = adapter.observe_target("api-secondary")

    _assert_code(
        "fault_host_api_state_mismatch",
        lambda: adapter.apply_action("api-secondary", "disable", before),
    )


def test_api_mutation_rechecks_exact_expected_state(tmp_path: Path) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_api_output(enabled=True)),
        _result(_api_output(enabled=True, container_id="c" * 64)),
    )
    expected = adapter.observe_target("api-secondary")

    with pytest.raises(FaultMutationPreconditionError) as captured:
        adapter.apply_action("api-secondary", "disable", expected)

    assert captured.value.code == "fault_host_api_state_mismatch"
    assert len(executor.calls) == 2


@pytest.mark.parametrize(
    ("publishers", "ports"),
    [
        (
            [
                {
                    "URL": "127.0.0.1",
                    "TargetPort": 8000,
                    "PublishedPort": 18000,
                    "Protocol": "tcp",
                }
            ],
            None,
        ),
        (None, "0.0.0.0:18000->8000/tcp"),
        (
            [{"URL": "", "TargetPort": 8001, "PublishedPort": 0, "Protocol": "tcp"}],
            None,
        ),
        (
            [{"URL": "", "TargetPort": 8000, "PublishedPort": 0, "Protocol": "udp"}],
            None,
        ),
        (
            [
                {"URL": "", "TargetPort": 8000, "PublishedPort": 0, "Protocol": "tcp"},
                {"URL": "", "TargetPort": 8000, "PublishedPort": 0, "Protocol": "tcp"},
            ],
            None,
        ),
    ],
)
def test_api_mutation_rejects_host_port_publication_drift(
    tmp_path: Path,
    publishers: list[dict[str, object]] | None,
    ports: str | None,
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_api_output(enabled=True)),
        _result(_api_output(enabled=True, publishers=publishers, ports=ports)),
    )
    expected = adapter.observe_target("api-secondary")

    _assert_code(
        "fault_host_api_identity_mismatch",
        lambda: adapter.apply_action("api-secondary", "disable", expected),
    )

    assert len(executor.calls) == 2


@pytest.mark.parametrize(
    ("publishers", "ports"),
    [
        (None, None),
        ([], ""),
        (
            [{"URL": "", "TargetPort": 8000, "PublishedPort": 0, "Protocol": "tcp"}],
            "8000/tcp",
        ),
    ],
)
def test_api_observation_accepts_only_internal_expose_variants(
    tmp_path: Path,
    publishers: list[dict[str, object]] | None,
    ports: str | None,
) -> None:
    adapter, _ = _adapter(
        tmp_path,
        _result(_api_output(enabled=True, publishers=publishers, ports=ports)),
    )

    assert adapter.observe_target("api-secondary").enabled is True


@pytest.mark.parametrize(
    "networks",
    [
        None,
        f"{PROJECT}_default,{PROJECT}_metrics",
        f"{PROJECT}_default,{PROJECT}_metrics,{PROJECT}_fault-data,{PROJECT}_extra",
        f"{PROJECT}_default,{PROJECT}_metrics,{PROJECT}_fault-control",
        f"{PROJECT}_default,{PROJECT}_metrics,{PROJECT}_fault-data,{PROJECT}_fault-control",
    ],
)
def test_api_mutation_rejects_network_membership_drift(
    tmp_path: Path,
    networks: str | None,
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_api_output(enabled=True)),
        _result(_api_output(enabled=True, networks=networks)),
    )
    expected = adapter.observe_target("api-secondary")

    with pytest.raises(FaultMutationPreconditionError) as captured:
        adapter.apply_action("api-secondary", "disable", expected)

    assert captured.value.code == "fault_host_api_identity_mismatch"
    assert len(executor.calls) == 2


def test_api_ps_jsonl_rejects_unknown_fields(tmp_path: Path) -> None:
    adapter, _ = _adapter(tmp_path, _result(_api_output(enabled=True, extra=True)))

    _assert_code("fault_host_api_state_invalid", lambda: adapter.observe_target("api-secondary"))


def test_api_ps_jsonl_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    ambiguous = _api_output(enabled=True).replace(
        b'"State":"running"', b'"State":"exited","State":"running"'
    )
    adapter, _ = _adapter(tmp_path, _result(ambiguous))

    _assert_code("fault_host_api_state_invalid", lambda: adapter.observe_target("api-secondary"))


def test_api_starting_state_is_typed_as_recovery_pending(tmp_path: Path) -> None:
    adapter, _ = _adapter(
        tmp_path,
        _result(_api_output(enabled=True, health="starting")),
    )

    with pytest.raises(FaultTargetRecoveryPendingError) as captured:
        adapter.observe_target("api-secondary")

    assert captured.value.code == "fault_host_api_recovery_pending"
    assert captured.value.target == "api-secondary"
    assert len(captured.value.identity_sha256) == 64


def test_api_enable_preserves_typed_starting_result_for_bounded_poll(
    tmp_path: Path,
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(_api_output(enabled=False)),
        _result(_api_output(enabled=False)),
        _result(),
        _result(_api_output(enabled=True, health="starting")),
    )
    expected = adapter.observe_target("api-secondary")

    with pytest.raises(FaultTargetRecoveryPendingError) as captured:
        adapter.apply_action("api-secondary", "enable", expected)

    assert captured.value.code == "fault_host_api_recovery_pending"
    assert captured.value.target == "api-secondary"
    assert captured.value.identity_sha256 == expected.identity_sha256
    assert executor.calls[2].argv[-5:] == (
        "start",
        "--wait",
        "--wait-timeout",
        "105",
        "api-secondary",
    )


def test_identity_binds_topology_and_exact_disable_recovery_commands(
    tmp_path: Path,
) -> None:
    adapter, _ = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
    )

    identity = build_fault_controller_identity(adapter.configuration, adapter)

    assert identity.context.run_id == RUN_ID
    assert identity.release == adapter.configuration.release_identity
    assert identity.topology.listen == "172.30.250.2:6380"
    assert identity.topology.upstream == "10.0.2.15:6380"
    assert identity.topology.docker_compose_version == "2.39.4"
    assert identity.topology.docker_engine_version == "28.0.1"
    assert identity.topology.fault_control_network_sha256 == (
        parse_fault_control_network_identity(_network_output())
    )
    assert identity.topology.toxiproxy_container_sha256 == (
        parse_toxiproxy_container(
            _toxiproxy_output(),
            expected_image=TOXIPROXY_IMAGE,
            expected_user=f"{os.geteuid()}:{os.getegid()}",
            expected_config_source=TOXIPROXY_CONFIG_SOURCE_PLACEHOLDER,
        ).identity_sha256
    )
    assert identity.topology.recovery_compose_sha256 == adapter.recovery_compose_sha256
    assert identity.topology.recovery_compose_sha256 in " ".join(adapter.compose_prefix())
    assert identity.topology.probe_command_sha256 == command_argv_sha256(
        adapter.fault_control_command("probe")
    )
    assert identity.topology.api_probe_command_sha256 == command_argv_sha256(
        adapter.api_state_command()
    )
    assert [(item.slot_number, item.target, item.action) for item in identity.commands] == [
        (1, "redis", "disable"),
        (2, "redis", "enable"),
        (3, "api-secondary", "disable"),
        (4, "api-secondary", "enable"),
    ]
    assert identity.commands[1].command_sha256 == adapter.recovery_command_sha256("redis")
    assert identity.commands[3].command_sha256 == adapter.recovery_command_sha256("api-secondary")


def test_compose_commands_use_validated_snapshot_after_release_is_mutated(
    tmp_path: Path,
) -> None:
    repository, candidate_path, rollback_path, candidate, _ = _configuration_files(tmp_path)
    configuration = load_fault_host_configuration(candidate_path, rollback_path, repository)
    snapshot_directory = tmp_path / "recovery-snapshot"
    snapshot_directory.mkdir(mode=0o700)
    snapshot_directory.chmod(0o700)
    recovery_snapshot = seal_fault_recovery_compose_snapshot(
        configuration,
        snapshot_directory,
    )
    executor = _FakeExecutor(_result(_api_output(enabled=True)))
    adapter = FaultHostAdapter(configuration, recovery_snapshot, executor)
    candidate["AWS_DEMO_API_IMAGE"] = "attacker-controlled"
    _write_secure(candidate_path, _serialize_release(candidate))

    observation = adapter.observe_target("api-secondary")
    recovery = adapter.recovery_command("api-secondary")

    assert observation.enabled is True
    assert "AWS_DEMO_API_IMAGE" not in executor.calls[0].environment
    assert str(candidate_path) not in executor.calls[0].argv
    assert "/dev/null" in executor.calls[0].argv
    assert str(recovery_snapshot.path) in executor.calls[0].argv
    assert str(recovery_snapshot.path) in recovery
    assert f"AWS_DEMO_API_IMAGE={API_IMAGE}" not in recovery
    assert "AWS_DEMO_API_IMAGE=attacker-controlled" not in recovery
    assert str(candidate_path) not in recovery


def test_adapter_rejects_unverified_recovery_snapshot_identity(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    snapshot_directory = tmp_path / "recovery-snapshot"
    snapshot_directory.mkdir(mode=0o700)
    snapshot_directory.chmod(0o700)
    recovery_snapshot = seal_fault_recovery_compose_snapshot(
        configuration,
        snapshot_directory,
    )
    forged = FaultRecoveryComposeSnapshot(
        path=recovery_snapshot.path,
        sha256="0" * 64,
        size_bytes=recovery_snapshot.size_bytes,
    )

    _assert_code(
        "fault_host_recovery_snapshot_invalid",
        lambda: FaultHostAdapter(configuration, forged, _FakeExecutor()),
    )
    recovery_snapshot.path.write_bytes(
        recovery_snapshot.path.read_bytes().replace(b'"probe"', b'"other"')
    )
    recovery_snapshot.path.chmod(0o600)
    _assert_code(
        "fault_host_recovery_snapshot_invalid",
        lambda: FaultHostAdapter(configuration, recovery_snapshot, _FakeExecutor()),
    )


@pytest.mark.parametrize("returncode", [0, 1])
def test_recovery_timer_arms_or_resumes_only_after_exact_readback(
    tmp_path: Path,
    returncode: int,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    executor.responses.extend([_result(returncode=returncode), _result(timer), _result(service)])

    observation = adapter.arm_recovery_timer(plan)

    assert observation.unit_name == plan.unit_name
    arm = executor.calls[0].argv
    assert "--timer-property=Persistent=no" in arm
    assert "--timer-property=RemainAfterElapse=no" in arm
    assert "--property=Type=oneshot" in arm
    assert any(value.startswith("--property=ExecCondition=/usr/bin/test -f ") for value in arm)
    assert any(
        value.startswith("--property=ExecStartPost=/usr/bin/mkdir --mode=0700 -- ") for value in arm
    )
    assert "--property=TimeoutStartSec=115s" in arm
    assert "--property=RuntimeMaxSec=115s" in arm
    assert "--property=KillMode=control-group" in arm
    assert "--property=SendSIGKILL=yes" in arm
    assert "--expand-environment=no" in arm
    separator = arm.index("--")
    assert arm[separator + 1 :] == adapter.recovery_command("redis")


def test_recovery_timer_rejects_property_drift(tmp_path: Path) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    timer = timer.replace(b"Persistent=no\n", b"Persistent=yes\n")
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


@pytest.mark.parametrize(
    "property_name",
    ["SuccessExitStatus", "RestartPreventExitStatus", "RestartForceExitStatus"],
)
def test_recovery_timer_rejects_success_exit_status_drift(
    tmp_path: Path,
    property_name: str,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    service = service.replace(
        f"{property_name}=\n".encode(),
        f"{property_name}=1\n".encode(),
    )
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (b"ignore_errors=no", b"ignore_errors=yes"),
        (b"flags=no-env-expand", b"flags="),
        (b"status=0/0 }", b"status=0/0 ; injected=x }"),
        (b"argv[]=/usr/bin/env", b"argv[]=/usr/bin/env ; argv[]=/usr/bin/env"),
    ],
)
def test_recovery_timer_rejects_exec_start_drift(
    tmp_path: Path,
    original: bytes,
    replacement: bytes,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    service = service.replace(original, replacement, 1)
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


def test_recovery_timer_rejects_extra_calendar_or_monotonic_trigger(
    tmp_path: Path,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    timer = timer.replace(
        b"TimersMonotonic=\n",
        b"TimersMonotonic={ OnActiveUSec=1s ; next_elapse=1s }\n",
    ).replace(b"NextElapseUSecMonotonic=0\n", b"NextElapseUSecMonotonic=1s\n")
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


def test_recovery_timer_rejects_multiple_calendar_triggers(tmp_path: Path) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    calendar_line = next(
        line for line in timer.splitlines(keepends=True) if line.startswith(b"TimersCalendar=")
    )
    timer = timer.replace(calendar_line, calendar_line.rstrip(b"\n") + b" " + calendar_line)
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


@pytest.mark.parametrize("property_name", ["OnClockChange", "OnTimezoneChange"])
def test_recovery_timer_rejects_clock_change_triggers(
    tmp_path: Path,
    property_name: str,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    timer = timer.replace(
        f"{property_name}=no\n".encode(),
        f"{property_name}=yes\n".encode(),
    )
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


@pytest.mark.parametrize(
    ("unit", "property_name"),
    [
        ("timer", "Conditions"),
        ("timer", "Asserts"),
        ("service", "Conditions"),
        ("service", "Asserts"),
    ],
)
def test_recovery_timer_rejects_conditions_and_asserts(
    tmp_path: Path,
    unit: str,
    property_name: str,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    changed = f"{property_name}=[ConditionPathExists=/tmp/unsafe]\n".encode()
    if unit == "timer":
        timer = timer.replace(f"{property_name}=\n".encode(), changed)
    else:
        service = service.replace(f"{property_name}=\n".encode(), changed)
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


@pytest.mark.parametrize(
    "property_name",
    [
        "ExecStartPre",
        "ExecReload",
        "ExecStop",
        "ExecStopPost",
    ],
)
def test_recovery_timer_rejects_service_execution_hooks(
    tmp_path: Path,
    property_name: str,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    service = service.replace(
        f"{property_name}=\n".encode(),
        f"{property_name}={{ path=/usr/bin/false ; argv[]=/usr/bin/false ; }}\n".encode(),
    )
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


@pytest.mark.parametrize("property_name", ["ExecCondition", "ExecStartPost"])
def test_recovery_timer_rejects_ownership_hook_drift(
    tmp_path: Path,
    property_name: str,
) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    line = next(
        value
        for value in service.splitlines(keepends=True)
        if value.startswith(f"{property_name}=".encode())
    )
    service = service.replace(
        line,
        f"{property_name}={{ path=/usr/bin/false ; argv[]=/usr/bin/false ; }}\n".encode(),
    )
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


def test_recovery_timer_rejects_exec_service_type_drift(tmp_path: Path) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    timer, service = _timer_readback(adapter, plan)
    service = service.replace(b"Type=oneshot\n", b"Type=exec\n")
    executor.responses.extend([_result(), _result(timer), _result(service)])

    _assert_code("fault_host_timer_readback_mismatch", lambda: adapter.arm_recovery_timer(plan))


def test_recovery_ownership_claim_is_exact_idempotent_and_revoke_is_idempotent(
    tmp_path: Path,
) -> None:
    adapter, _ = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    marker = directory / f"slot-{plan.slot_number:02d}-{plan.target}.owned"

    assert adapter.claim_recovery_ownership(plan) is True
    original = marker.read_bytes()
    assert adapter.claim_recovery_ownership(plan) is False
    assert marker.read_bytes() == original
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert marker.stat().st_uid == os.geteuid()
    assert marker.stat().st_gid == os.getegid()

    adapter.revoke_recovery_ownership(plan)
    adapter.revoke_recovery_ownership(plan)
    assert not marker.exists()


def test_recovery_ownership_partial_write_never_publishes_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    marker = directory / f"slot-{plan.slot_number:02d}-{plan.target}.owned"
    monkeypatch.setattr(ownership_module.os, "write", lambda *_args: 0)

    _assert_code(
        "fault_host_recovery_ownership_write_failed",
        lambda: adapter.claim_recovery_ownership(plan),
    )

    assert not marker.exists()


def test_recovery_ownership_rejects_tampered_resume_marker(tmp_path: Path) -> None:
    adapter, _ = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    assert adapter.claim_recovery_ownership(plan) is True
    directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    marker = directory / f"slot-{plan.slot_number:02d}-{plan.target}.owned"
    marker.write_bytes(b"tampered\n")
    marker.chmod(0o600)

    _assert_code(
        "fault_host_recovery_ownership_invalid",
        lambda: adapter.claim_recovery_ownership(plan),
    )


def test_recovery_receipt_requires_atomic_empty_secure_directory(
    tmp_path: Path,
) -> None:
    adapter, _ = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    assert adapter.claim_recovery_ownership(plan) is True
    directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    receipt_path = directory / f"slot-{plan.slot_number:02d}-{plan.target}.receipt"
    receipt_path.mkdir(mode=0o700)
    recorded_at = plan.trigger_at + timedelta(seconds=1)
    timestamp_ns = int(recorded_at.timestamp() * 1_000_000_000)
    os.utime(receipt_path, ns=(timestamp_ns, timestamp_ns), follow_symlinks=False)

    assert adapter.read_recovery_receipt(plan, plan.trigger_at) is None
    receipt = adapter.read_recovery_receipt(
        plan,
        plan.trigger_at + timedelta(seconds=2),
    )

    assert receipt is not None
    assert receipt.unit_name == plan.unit_name
    assert receipt.trigger_at == plan.trigger_at
    assert receipt.recorded_at == recorded_at
    assert len(receipt.receipt_sha256) == 64


@pytest.mark.parametrize("invalid_shape", ["file", "mode", "nonempty", "symlink"])
def test_recovery_receipt_rejects_insecure_shape(
    tmp_path: Path,
    invalid_shape: str,
) -> None:
    adapter, _ = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    assert adapter.claim_recovery_ownership(plan) is True
    directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    receipt_path = directory / f"slot-{plan.slot_number:02d}-{plan.target}.receipt"
    if invalid_shape == "file":
        receipt_path.write_bytes(b"")
        receipt_path.chmod(0o600)
    elif invalid_shape == "symlink":
        target = tmp_path / "receipt-target"
        target.mkdir(mode=0o700)
        receipt_path.symlink_to(target, target_is_directory=True)
    else:
        receipt_path.mkdir(mode=0o700)
        if invalid_shape == "mode":
            receipt_path.chmod(0o755)
        else:
            (receipt_path / "unexpected").write_bytes(b"")

    _assert_code(
        "fault_host_recovery_receipt_invalid",
        lambda: adapter.read_recovery_receipt(
            plan,
            plan.trigger_at + timedelta(seconds=1),
        ),
    )


def test_recovery_timer_refuses_preexisting_receipt(tmp_path: Path) -> None:
    adapter, executor = _adapter(tmp_path)
    plan = _recovery_plan(adapter, "redis")
    assert adapter.claim_recovery_ownership(plan) is True
    directory = (
        adapter.configuration.home_directory
        / f".gs-loadtest-fault-{adapter.configuration.manifest.run_id.hex}"
    )
    receipt_path = directory / f"slot-{plan.slot_number:02d}-{plan.target}.receipt"
    receipt_path.mkdir(mode=0o700)

    _assert_code(
        "fault_host_recovery_receipt_invalid",
        lambda: adapter.arm_recovery_timer(plan),
    )

    assert executor.calls == []


def test_recovery_timer_plan_is_derived_from_manifest_schedule(tmp_path: Path) -> None:
    adapter, _ = _adapter(tmp_path)
    valid = _recovery_plan(adapter, "api-secondary")
    invalid = valid.model_copy(update={"slot_number": 3})

    _assert_code("fault_host_timer_plan_invalid", lambda: adapter.read_recovery_timer(invalid))


def test_subprocess_executor_drops_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_CANARY", "must-not-propagate")
    executor = SubprocessCommandExecutor()

    result = executor.run(
        (
            sys.executable,
            "-c",
            "import os;print(os.getenv('ONLY'));print(os.getenv('SECRET_CANARY'))",
        ),
        environment={"ONLY": "expected"},
        timeout_seconds=2,
    )

    assert result.returncode == 0
    assert result.stdout == b"expected\nNone\n"
    assert result.stderr == b""


def test_subprocess_kill_does_not_wait_without_bound_after_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornProcess:
        pid = 12345

        def __init__(self) -> None:
            self.wait_timeouts: list[float | None] = []
            self.kill_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired("stubborn", timeout)

        def kill(self) -> None:
            self.kill_calls += 1

    process = StubbornProcess()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    SubprocessCommandExecutor._kill(cast(subprocess.Popen[bytes], process))

    assert signals == [(process.pid, signal.SIGKILL)]
    assert process.kill_calls == 1
    assert process.wait_timeouts == [1, 1]


def test_subprocess_executor_enforces_output_and_time_bounds() -> None:
    output_executor = SubprocessCommandExecutor(maximum_output_bytes=16)
    _assert_code(
        "fault_host_command_output_too_large",
        lambda: output_executor.run(
            (sys.executable, "-c", "print('x' * 100)"),
            environment={},
            timeout_seconds=2,
        ),
    )
    timeout_executor = SubprocessCommandExecutor()
    _assert_code(
        "fault_host_command_timeout",
        lambda: timeout_executor.run(
            (sys.executable, "-c", "import time;time.sleep(2)"),
            environment={},
            timeout_seconds=0.05,
        ),
    )


def test_fresh_formal_environment_allows_same_image_recovery_release(tmp_path: Path) -> None:
    repository, candidate_path, rollback_path, candidate, rollback = _configuration_files(tmp_path)
    for field in ("AWS_DEMO_API_IMAGE", "AWS_DEMO_GATEWAY_IMAGE", "AWS_DEMO_SOURCE_COMMIT"):
        rollback[field] = candidate[field]
    _write_secure(rollback_path, _serialize_release(rollback))
    configuration = load_fault_host_configuration(candidate_path, rollback_path, repository)
    assert configuration.manifest.format == "load-test-manifest-v3"


def test_fault_release_rejects_retired_event_slug_field(tmp_path: Path) -> None:
    repository, candidate_path, rollback_path, candidate, _ = _configuration_files(tmp_path)
    candidate["AWS_DEMO_EVENT_SLUG"] = "load-test-2030-01-01"
    _write_secure(candidate_path, _serialize_release(candidate))
    with pytest.raises(FaultControllerHostError):
        load_fault_host_configuration(candidate_path, rollback_path, repository)
