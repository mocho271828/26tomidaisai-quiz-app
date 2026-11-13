from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from quiz_app import aws_demo_paths as aws_demo_paths_module
from quiz_app.aws_demo_paths import AwsDemoDeploymentValidationError
from quiz_app.dependency_hosts import ValidatedDependencyHost
from quiz_app.load_test.fault_topology import (
    TOXIPROXY_CONFIG_MAX_BYTES,
    TOXIPROXY_CONTROL_ADDRESS,
    TOXIPROXY_CONTROL_NETWORK,
    TOXIPROXY_CONTROL_PORT,
    TOXIPROXY_DATA_ADDRESS,
    TOXIPROXY_DATA_NETWORK,
    TOXIPROXY_EXPECTED_REVISION,
    TOXIPROXY_EXPECTED_VERSION,
    TOXIPROXY_PROXY_NAME,
    ValidatedFaultTopology,
    load_runtime_fault_topology,
    validate_external_fault_topology,
    validate_external_toxiproxy_proxy,
    validate_fault_topology_database_route,
    validate_fault_topology_dns,
)

REDIS_HOSTNAME = "redis.aws.internal"
REDIS_PORT = 6380
REDIS_URL = f"rediss://load-user:secret@{REDIS_HOSTNAME}:{REDIS_PORT}/0"
UPSTREAM_ADDRESS = "10.0.2.15"


def _proxy(**overrides: object) -> dict[str, object]:
    proxy: dict[str, object] = {
        "name": TOXIPROXY_PROXY_NAME,
        "listen": f"{TOXIPROXY_DATA_ADDRESS}:{REDIS_PORT}",
        "upstream": f"{UPSTREAM_ADDRESS}:{REDIS_PORT}",
        "enabled": True,
    }
    proxy.update(overrides)
    return proxy


def _body(*proxies: dict[str, object]) -> bytes:
    values = proxies or (_proxy(),)
    return (json.dumps(values, separators=(",", ":")) + "\n").encode()


def _write_config(directory: Path, body: bytes | None = None) -> Path:
    config_path = directory / "toxiproxy.json"
    config_path.write_bytes(_body() if body is None else body)
    config_path.chmod(0o600)
    return config_path


def _validate(
    tmp_path: Path,
    *,
    body: bytes | None = None,
    redis_url: str = REDIS_URL,
    expected_hostname: str = REDIS_HOSTNAME,
    expected_sha256: str | None = None,
) -> ValidatedFaultTopology:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    external_directory = tmp_path / "external"
    external_directory.mkdir(mode=0o700)
    external_directory.chmod(0o700)
    config_path = _write_config(external_directory, body)
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return validate_external_fault_topology(
        config_path,
        digest if expected_sha256 is None else expected_sha256,
        repository_root,
        redis_url,
        expected_hostname,
    )


def _assert_code(expected_code: str, operation: Callable[[], object]) -> None:
    with pytest.raises(AwsDemoDeploymentValidationError) as captured:
        operation()
    assert captured.value.code == expected_code


def test_fault_topology_constants_are_release_pinned() -> None:
    assert TOXIPROXY_EXPECTED_VERSION == "2.12.0"
    assert TOXIPROXY_EXPECTED_REVISION == "3ccd6a79cbc6c6a72b884d295ad314b75cdf3962"
    assert TOXIPROXY_DATA_ADDRESS in TOXIPROXY_DATA_NETWORK
    assert TOXIPROXY_CONTROL_ADDRESS in TOXIPROXY_CONTROL_NETWORK
    assert TOXIPROXY_CONTROL_PORT == 8474


def test_external_fault_topology_accepts_exact_contract(tmp_path: Path) -> None:
    topology = _validate(tmp_path)

    assert topology.config_path == (tmp_path / "external/toxiproxy.json").resolve()
    assert topology.redis_hostname == REDIS_HOSTNAME
    assert topology.redis_port == REDIS_PORT
    assert topology.upstream_address == UPSTREAM_ADDRESS
    assert topology.proxy.name == TOXIPROXY_PROXY_NAME
    assert topology.proxy.enabled is True


def test_external_proxy_identity_does_not_require_redis_credentials(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    external_directory = tmp_path / "external"
    external_directory.mkdir(mode=0o700)
    external_directory.chmod(0o700)
    config_path = _write_config(external_directory)
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()

    proxy = validate_external_toxiproxy_proxy(config_path, digest, repository_root)

    assert proxy.model_dump() == _proxy()


def test_external_fault_topology_opens_config_with_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        pytest.skip("O_NOFOLLOW is not supported on this platform")
    original_open = os.open
    observed_flags: list[int] = []

    def capture_open(path: Path, flags: int) -> int:
        observed_flags.append(flags)
        return original_open(path, flags)

    monkeypatch.setattr(aws_demo_paths_module.os, "open", capture_open)

    _validate(tmp_path)

    assert observed_flags
    assert all(flags & no_follow for flags in observed_flags)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (_body(_proxy(unknown="rejected")), "toxiproxy_config_invalid"),
        (_body(_proxy(), _proxy()), "toxiproxy_config_invalid"),
        (_body(_proxy(enabled=False)), "toxiproxy_config_invalid"),
        (_body(_proxy(name="another-proxy")), "toxiproxy_config_invalid"),
        (
            (
                b'[{"name":"gs-app-tomidaisai-aws-demo-redis",'
                b'"listen":"172.30.250.2:6380","upstream":"10.0.2.14:6380",'
                b'"upstream":"10.0.2.15:6380","enabled":true}]\n'
            ),
            "toxiproxy_config_invalid",
        ),
        (b"{}\n", "toxiproxy_config_invalid"),
        (b"[]\n", "toxiproxy_config_invalid"),
    ],
)
def test_fault_topology_rejects_non_exact_json_contract(
    tmp_path: Path,
    body: bytes,
    code: str,
) -> None:
    _assert_code(code, lambda: _validate(tmp_path, body=body))


@pytest.mark.parametrize(
    "upstream",
    [
        f"redis.aws.internal:{REDIS_PORT}",
        f"127.0.0.1:{REDIS_PORT}",
        f"0.0.0.0:{REDIS_PORT}",
        f"{TOXIPROXY_DATA_ADDRESS}:{REDIS_PORT}",
        f"{TOXIPROXY_CONTROL_ADDRESS}:{REDIS_PORT}",
        f"[fd00::15]:{REDIS_PORT}",
    ],
)
def test_fault_topology_rejects_non_remote_literal_ipv4_upstream(
    tmp_path: Path,
    upstream: str,
) -> None:
    _assert_code(
        "toxiproxy_config_upstream_invalid",
        lambda: _validate(tmp_path, body=_body(_proxy(upstream=upstream))),
    )


@pytest.mark.parametrize(
    "listen",
    [
        f"0.0.0.0:{REDIS_PORT}",
        f"127.0.0.1:{REDIS_PORT}",
        f"172.30.250.3:{REDIS_PORT}",
        f"redis-proxy:{REDIS_PORT}",
    ],
)
def test_fault_topology_rejects_non_fixed_listen_address(
    tmp_path: Path,
    listen: str,
) -> None:
    _assert_code(
        "toxiproxy_config_listen_invalid",
        lambda: _validate(tmp_path, body=_body(_proxy(listen=listen))),
    )


@pytest.mark.parametrize(
    "proxy",
    [
        _proxy(listen=f"{TOXIPROXY_DATA_ADDRESS}:6379"),
        _proxy(upstream=f"{UPSTREAM_ADDRESS}:6379"),
    ],
)
def test_fault_topology_rejects_port_mismatch(
    tmp_path: Path,
    proxy: dict[str, object],
) -> None:
    _assert_code(
        "toxiproxy_config_port_mismatch",
        lambda: _validate(tmp_path, body=_body(proxy)),
    )


@pytest.mark.parametrize(
    ("redis_url", "hostname", "code"),
    [
        (
            f"redis://load-user:secret@{REDIS_HOSTNAME}:{REDIS_PORT}/0",
            REDIS_HOSTNAME,
            "fault_topology_redis_url_invalid",
        ),
        (
            REDIS_URL,
            "different.aws.internal",
            "toxiproxy_redis_hostname_mismatch",
        ),
        (
            f"rediss://load-user:secret@10.0.2.15:{REDIS_PORT}/0",
            "10.0.2.15",
            "fault_topology_redis_hostname_invalid",
        ),
        (
            f"rediss://load-user:secret@redis:{REDIS_PORT}/0",
            "redis",
            "fault_topology_redis_hostname_invalid",
        ),
    ],
)
def test_fault_topology_rejects_invalid_runtime_redis_endpoint(
    tmp_path: Path,
    redis_url: str,
    hostname: str,
    code: str,
) -> None:
    _assert_code(
        code,
        lambda: _validate(tmp_path, redis_url=redis_url, expected_hostname=hostname),
    )


@pytest.mark.parametrize(
    ("expected_sha256", "code"),
    [
        ("a" * 63, "toxiproxy_config_sha256_invalid"),
        ("A" * 64, "toxiproxy_config_sha256_invalid"),
        ("0" * 64, "toxiproxy_config_checksum_mismatch"),
    ],
)
def test_external_fault_topology_rejects_invalid_checksum(
    tmp_path: Path,
    expected_sha256: str,
    code: str,
) -> None:
    _assert_code(
        code,
        lambda: _validate(tmp_path, expected_sha256=expected_sha256),
    )


def test_external_fault_topology_rejects_non_0600_config(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    external_directory = tmp_path / "external"
    external_directory.mkdir(mode=0o700)
    config_path = _write_config(external_directory)
    config_path.chmod(0o640)

    _assert_code(
        "toxiproxy_config_permissions_not_0600",
        lambda: validate_external_fault_topology(
            config_path,
            hashlib.sha256(config_path.read_bytes()).hexdigest(),
            repository_root,
            REDIS_URL,
            REDIS_HOSTNAME,
        ),
    )


def test_external_fault_topology_rejects_symlink(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    external_directory = tmp_path / "external"
    external_directory.mkdir(mode=0o700)
    target = _write_config(external_directory)
    alias = external_directory / "alias.json"
    alias.symlink_to(target)

    _assert_code(
        "toxiproxy_config_symlink_forbidden",
        lambda: validate_external_fault_topology(
            alias,
            hashlib.sha256(target.read_bytes()).hexdigest(),
            repository_root,
            REDIS_URL,
            REDIS_HOSTNAME,
        ),
    )


def test_external_fault_topology_rejects_hardlink(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    external_directory = tmp_path / "external"
    external_directory.mkdir(mode=0o700)
    config_path = _write_config(external_directory)
    os.link(config_path, external_directory / "alias.json")

    _assert_code(
        "toxiproxy_config_hardlink_forbidden",
        lambda: validate_external_fault_topology(
            config_path,
            hashlib.sha256(config_path.read_bytes()).hexdigest(),
            repository_root,
            REDIS_URL,
            REDIS_HOSTNAME,
        ),
    )


def test_external_fault_topology_rejects_oversized_config(tmp_path: Path) -> None:
    oversized_body = b"[" + b" " * TOXIPROXY_CONFIG_MAX_BYTES + b"]"

    _assert_code(
        "toxiproxy_config_unavailable",
        lambda: _validate(tmp_path, body=oversized_body),
    )


def test_runtime_loader_revalidates_fixed_contract(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()

    topology = load_runtime_fault_topology(REDIS_URL, digest, config_path)

    assert topology.config_path == config_path.resolve()
    assert topology.redis_hostname == REDIS_HOSTNAME
    assert topology.upstream_address == UPSTREAM_ADDRESS


def test_runtime_loader_rejects_symlink(tmp_path: Path) -> None:
    target = _write_config(tmp_path)
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)

    _assert_code(
        "toxiproxy_config_unavailable",
        lambda: load_runtime_fault_topology(
            REDIS_URL,
            hashlib.sha256(target.read_bytes()).hexdigest(),
            alias,
        ),
    )


def test_fault_topology_dns_accepts_exact_redis_binding(tmp_path: Path) -> None:
    topology = _validate(tmp_path)
    validated_host = ValidatedDependencyHost(
        dependency="redis",
        hostname=REDIS_HOSTNAME,
        resolved_addresses=(UPSTREAM_ADDRESS,),
        selected_address=UPSTREAM_ADDRESS,
    )

    validate_fault_topology_dns(topology, validated_host)


@pytest.mark.parametrize(
    "validated_host",
    [
        ValidatedDependencyHost(
            dependency="database",
            hostname=REDIS_HOSTNAME,
            resolved_addresses=(UPSTREAM_ADDRESS,),
            selected_address=UPSTREAM_ADDRESS,
        ),
        ValidatedDependencyHost(
            dependency="redis",
            hostname="different.aws.internal",
            resolved_addresses=(UPSTREAM_ADDRESS,),
            selected_address=UPSTREAM_ADDRESS,
        ),
        ValidatedDependencyHost(
            dependency="redis",
            hostname=REDIS_HOSTNAME,
            resolved_addresses=("10.0.2.16",),
            selected_address="10.0.2.16",
        ),
        ValidatedDependencyHost(
            dependency="redis",
            hostname=REDIS_HOSTNAME,
            resolved_addresses=(),
            selected_address=UPSTREAM_ADDRESS,
        ),
    ],
)
def test_fault_topology_dns_rejects_mismatched_binding(
    tmp_path: Path,
    validated_host: ValidatedDependencyHost,
) -> None:
    topology = _validate(tmp_path)

    _assert_code(
        "fault_topology_dns_mismatch",
        lambda: validate_fault_topology_dns(topology, validated_host),
    )


def test_fault_topology_dns_rejects_selected_address_missing_from_resolution(
    tmp_path: Path,
) -> None:
    topology = _validate(tmp_path)
    validated_host = ValidatedDependencyHost(
        dependency="redis",
        hostname=REDIS_HOSTNAME,
        resolved_addresses=("10.0.2.16",),
        selected_address=UPSTREAM_ADDRESS,
    )

    _assert_code(
        "fault_topology_dns_mismatch",
        lambda: validate_fault_topology_dns(topology, validated_host),
    )


def test_fault_topology_dns_rejects_additional_resolved_address(tmp_path: Path) -> None:
    topology = _validate(tmp_path)
    validated_host = ValidatedDependencyHost(
        dependency="redis",
        hostname=REDIS_HOSTNAME,
        resolved_addresses=(UPSTREAM_ADDRESS, "10.0.2.16"),
        selected_address=UPSTREAM_ADDRESS,
    )

    _assert_code(
        "fault_topology_dns_mismatch",
        lambda: validate_fault_topology_dns(topology, validated_host),
    )


def test_fault_topology_database_route_accepts_non_overlapping_host() -> None:
    validate_fault_topology_database_route(
        ValidatedDependencyHost(
            dependency="database",
            hostname="db.aws.internal",
            resolved_addresses=("10.0.1.15",),
            selected_address="10.0.1.15",
        )
    )


@pytest.mark.parametrize(
    "validated_host",
    [
        ValidatedDependencyHost(
            dependency="redis",
            hostname=REDIS_HOSTNAME,
            resolved_addresses=(UPSTREAM_ADDRESS,),
            selected_address=UPSTREAM_ADDRESS,
        ),
        ValidatedDependencyHost(
            dependency="database",
            hostname="db.aws.internal",
            resolved_addresses=("172.30.250.3",),
            selected_address="172.30.250.3",
        ),
        ValidatedDependencyHost(
            dependency="database",
            hostname="db.aws.internal",
            resolved_addresses=("::ffff:172.30.250.3",),
            selected_address="::ffff:172.30.250.3",
        ),
    ],
)
def test_fault_topology_database_route_rejects_invalid_binding(
    validated_host: ValidatedDependencyHost,
) -> None:
    _assert_code(
        "fault_topology_database_route_collision",
        lambda: validate_fault_topology_database_route(validated_host),
    )


def test_validated_topology_is_immutable(tmp_path: Path) -> None:
    topology = _validate(tmp_path)

    changed = replace(topology, upstream_address="10.0.2.16")

    assert topology.upstream_address == UPSTREAM_ADDRESS
    assert changed.upstream_address == "10.0.2.16"
