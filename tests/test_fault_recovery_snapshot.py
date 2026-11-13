from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from quiz_app.load_test.fault_controller_contracts import FaultReleaseIdentity
from quiz_app.load_test.fault_host_config import (
    FaultControllerHostError,
    FaultHostConfiguration,
)
from quiz_app.load_test.fault_recovery_snapshot import (
    API_SECONDARY_SERVICE,
    COMPOSE_PROJECT,
    FAULT_CONTROL_NETWORK,
    FAULT_CONTROL_NETWORK_NAME,
    FAULT_CONTROL_SERVICE,
    decode_fault_recovery_compose_snapshot,
    seal_fault_recovery_compose_snapshot,
    validate_fault_recovery_compose_snapshot,
)
from quiz_app.load_test.fault_topology import ToxiproxyRedisProxy
from quiz_app.load_test.manifest import build_load_test_manifest

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_COMMIT = "a" * 40
API_IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
GATEWAY_IMAGE = f"registry.aws.internal/quiz/gateway@sha256:{'2' * 64}"
GENERATOR_IMAGE = f"registry.aws.internal/quiz/generator@sha256:{'3' * 64}"
TOXIPROXY_SHA256 = "4" * 64


def _configuration(tmp_path: Path) -> FaultHostConfiguration:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    manifest = build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2030-01-01",
        scheduled_start_at=datetime(2030, 1, 1, tzinfo=UTC) + timedelta(hours=1),
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
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    return FaultHostConfiguration(
        repository_root=repository,
        release_file=external / "candidate.env",
        rollback_file=external / "rollback.env",
        manifest_file=external / "manifest.json",
        manifest=manifest,
        manifest_sha256="5" * 64,
        release_identity=FaultReleaseIdentity(
            release_id="release-candidate",
            release_manifest_sha256="6" * 64,
            source_commit=SOURCE_COMMIT,
            rollback_release_id="release-rollback",
            rollback_manifest_sha256="7" * 64,
            execution_host="load-host.aws.internal",
            docker_context="aws-load-host",
        ),
        release_environment=(),
        rollback_environment=(),
        execution_host="load-host.aws.internal",
        docker_context="aws-load-host",
        docker_endpoint="unix:///var/run/docker.sock",
        api_image=API_IMAGE,
        toxiproxy_image="registry.aws.internal/quiz/toxiproxy@sha256:" + "7" * 64,
        toxiproxy_configuration_file=tmp_path / "external" / "toxiproxy.json",
        redis_hostname="redis.aws.internal",
        toxiproxy_proxy=ToxiproxyRedisProxy(
            name="gs-app-tomidaisai-aws-demo-redis",
            listen="172.30.250.2:6380",
            upstream="10.0.2.15:6380",
            enabled=True,
        ),
        toxiproxy_configuration_sha256=TOXIPROXY_SHA256,
        home_directory=tmp_path,
    )


def _snapshot_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "recovery-snapshot"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _assert_code(expected: str, operation: object) -> None:
    with pytest.raises(FaultControllerHostError) as captured:
        operation()  # type: ignore[operator]
    assert captured.value.code == expected


def test_snapshot_seals_minimal_literal_secret_free_compose(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    snapshot = seal_fault_recovery_compose_snapshot(
        configuration,
        _snapshot_directory(tmp_path),
    )

    body = snapshot.path.read_bytes()
    document = decode_fault_recovery_compose_snapshot(body)
    status = snapshot.path.stat()

    assert snapshot.sha256 == hashlib.sha256(body).hexdigest()
    assert snapshot.sha256 in snapshot.path.name
    assert snapshot.size_bytes == len(body)
    assert stat.S_IMODE(status.st_mode) == 0o600
    assert status.st_nlink == 1
    assert set(document) == {"name", "networks", "services"}
    assert document["name"] == COMPOSE_PROJECT
    assert document["networks"] == {
        FAULT_CONTROL_NETWORK: {
            "external": True,
            "name": FAULT_CONTROL_NETWORK_NAME,
        }
    }
    services = document["services"]
    assert isinstance(services, dict)
    assert set(services) == {API_SECONDARY_SERVICE, FAULT_CONTROL_SERVICE}
    assert services[API_SECONDARY_SERVICE] == {
        "image": API_IMAGE,
        "pull_policy": "never",
    }
    client = services[FAULT_CONTROL_SERVICE]
    assert isinstance(client, dict)
    assert client["environment"] == {
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256": TOXIPROXY_SHA256,
        "AWS_DEMO_TOXIPROXY_EXPECTED_LISTEN": "172.30.250.2:6380",
        "AWS_DEMO_TOXIPROXY_EXPECTED_NAME": ("gs-app-tomidaisai-aws-demo-redis"),
        "AWS_DEMO_TOXIPROXY_EXPECTED_UPSTREAM": "10.0.2.15:6380",
    }
    assert client["profiles"] == ["fault-control"]
    assert client["networks"] == [FAULT_CONTROL_NETWORK]
    assert client["read_only"] is True
    assert client["cap_drop"] == ["ALL"]
    assert client["security_opt"] == ["no-new-privileges:true"]
    assert "volumes" not in client
    assert "env_file" not in client
    assert "secrets" not in client
    assert "configs" not in client
    assert "${" not in body.decode("ascii")
    assert "bind" not in body.decode("ascii")


def test_snapshot_resume_accepts_only_exact_existing_bytes(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    directory = _snapshot_directory(tmp_path)
    first = seal_fault_recovery_compose_snapshot(configuration, directory)

    assert seal_fault_recovery_compose_snapshot(configuration, directory) == first
    first.path.write_bytes(first.path.read_bytes().replace(b'"probe"', b'"other"'))
    first.path.chmod(0o600)

    _assert_code(
        "fault_host_recovery_snapshot_invalid",
        lambda: seal_fault_recovery_compose_snapshot(configuration, directory),
    )


def test_snapshot_io_uses_exclusive_create_and_explicit_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _configuration(tmp_path)
    directory = _snapshot_directory(tmp_path)
    real_open = os.open
    real_read = os.read
    real_write = os.write
    creation_flags: list[int] = []
    read_calls = 0
    write_calls = 0

    def partial_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, str) and path.endswith(".compose.json"):
            creation_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    def partial_read(descriptor: int, amount: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return real_read(descriptor, min(amount, 7))

    def partial_write(descriptor: int, body: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        return real_write(descriptor, body[:11])

    monkeypatch.setattr(os, "open", partial_open)
    monkeypatch.setattr(os, "read", partial_read)
    monkeypatch.setattr(os, "write", partial_write)

    snapshot = seal_fault_recovery_compose_snapshot(configuration, directory)

    assert validate_fault_recovery_compose_snapshot(configuration, snapshot) == snapshot
    assert any(flags & os.O_EXCL for flags in creation_flags)
    assert read_calls > 2
    assert write_calls > 2


@pytest.mark.parametrize("mode", [0o755, 0o770])
def test_snapshot_rejects_non_private_directory(tmp_path: Path, mode: int) -> None:
    configuration = _configuration(tmp_path)
    directory = _snapshot_directory(tmp_path)
    directory.chmod(mode)

    _assert_code(
        "fault_host_recovery_snapshot_directory_invalid",
        lambda: seal_fault_recovery_compose_snapshot(configuration, directory),
    )


def test_snapshot_rejects_repository_or_nonempty_directory(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    repository_child = configuration.repository_root / "snapshot"
    repository_child.mkdir(mode=0o700)
    external = _snapshot_directory(tmp_path)
    (external / "unexpected").touch()

    _assert_code(
        "fault_host_recovery_snapshot_directory_invalid",
        lambda: seal_fault_recovery_compose_snapshot(configuration, repository_child),
    )
    _assert_code(
        "fault_host_recovery_snapshot_directory_invalid",
        lambda: seal_fault_recovery_compose_snapshot(configuration, external),
    )


@pytest.mark.parametrize("target", ["directory", "file"])
def test_snapshot_rejects_group_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    configuration = _configuration(tmp_path)
    directory = _snapshot_directory(tmp_path)
    snapshot = seal_fault_recovery_compose_snapshot(configuration, directory)
    real_fstat = os.fstat

    def mismatched_fstat(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        is_target = (target == "directory" and stat.S_ISDIR(status.st_mode)) or (
            target == "file" and stat.S_ISREG(status.st_mode)
        )
        if not is_target:
            return status
        values = {
            attribute: getattr(status, attribute)
            for attribute in dir(status)
            if attribute.startswith("st_")
        }
        values["st_gid"] = os.getegid() + 1
        return SimpleNamespace(**values)  # type: ignore[return-value]

    monkeypatch.setattr(os, "fstat", mismatched_fstat)

    expected_code = (
        "fault_host_recovery_snapshot_directory_invalid"
        if target == "directory"
        else "fault_host_recovery_snapshot_invalid"
    )
    _assert_code(
        expected_code,
        lambda: validate_fault_recovery_compose_snapshot(configuration, snapshot),
    )
