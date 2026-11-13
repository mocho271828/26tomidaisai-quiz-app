from __future__ import annotations

from pathlib import Path

import pytest
from test_fault_controller_host import (
    DOCKER_COMPOSE_VERSION,
    DOCKER_CONFIG_DIRECTORY,
    DOCKER_ENGINE_VERSION,
    EXECUTION_HOST,
    _adapter,
    _network_output,
    _result,
    _toxiproxy_output,
)

from quiz_app.load_test.fault_host_config import FaultControllerHostError


@pytest.fixture(autouse=True)
def _docker_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "quiz_app.load_test.fault_host_commands.require_root_owned_docker_config_directory",
        lambda: Path(DOCKER_CONFIG_DIRECTORY),
    )


@pytest.mark.parametrize(
    ("cli_version", "manager_version", "major"),
    [
        (b"systemd 255 (255.4-1ubuntu8.17)\n", b"255.4-1ubuntu8.17\n", 255),
        (b"systemd 255 (255.4-1ubuntu8)\n", b"255.4-1ubuntu8\n", 255),
        (b"systemd 255 (255.4)\n", b"255.4\n", 255),
        (b"systemd 255 (255)\n", b"255\n", 255),
        (b"systemd 254 (254.1)\n", b"254.1\n", 254),
        (b"systemd 254 (254.1)\n", b"254 (254.1)\n", 254),
    ],
)
def test_preflight_accepts_matching_upstream_and_distribution_versions(
    tmp_path: Path, cli_version: bytes, manager_version: bytes, major: int
) -> None:
    # 公式Manager.VersionはGIT_VERSION。Nobleのpackage版にはUbuntuのrevisionが付く。
    # https://github.com/systemd/systemd/blob/v255/src/core/dbus-manager.c
    # https://packages.ubuntu.com/noble/systemd
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(cli_version),
        _result(manager_version),
        _result(b"yes\n"),
        _result(b"yes\n"),
        _result(b"active\n"),
        _result(f"{EXECUTION_HOST}\n".encode()),
        _result(b"unix:///var/run/docker.sock\n"),
    )

    observation = adapter.preflight()

    assert observation.systemd_version == major
    assert observation.ntp_synchronized and observation.linger_enabled
    assert observation.user_manager_active
    assert not executor.responses


@pytest.mark.parametrize(
    ("cli_version", "manager_version", "expected_calls"),
    [
        (b"systemd 253 (253.9)\n", b"253.9\n", 5),
        (b"systemd 255 (255.4)\n", b"254.9-1ubuntu1\n", 6),
        (b"systemd 255 (255.4)\n", b"256.1\n", 6),
        (b"systemd 255 (255.4)\n", b"2550.1\n", 6),
        (b"systemd 255 (255.4)\n", b"255.4-1ubuntu8\nextra\n", 6),
        (b"systemd 255 (255.4)\n", b"255.4-1ubuntu8", 6),
        (b"systemd 255 (255.4)\n", b"systemd 255 (255.4)\n", 6),
        (b"systemd 255 (255.4)\n", b"255.4\x00\n", 6),
        (b"systemd 255 (255.4)\n", b"255.4;false\n", 6),
        (b"systemd 255 (255.4)\n", b"255.4 extra\n", 6),
        (b"systemd 255 (255.4)\n", b"255.4\r\n", 6),
        (b"systemd 255 (255.4)\n", b"255.\n", 6),
        (b"systemd 255 (255.4)\n", b"255.---\n", 6),
        (b"systemd 255 (255.4)\n", b"255." + b"1" * 128 + b"\n", 6),
    ],
)
def test_preflight_rejects_old_mismatched_or_ambiguous_versions(
    tmp_path: Path, cli_version: bytes, manager_version: bytes, expected_calls: int
) -> None:
    adapter, executor = _adapter(
        tmp_path,
        _result(DOCKER_COMPOSE_VERSION),
        _result(DOCKER_ENGINE_VERSION),
        _result(_network_output()),
        _result(_toxiproxy_output()),
        _result(cli_version),
        _result(manager_version),
    )

    with pytest.raises(FaultControllerHostError) as caught:
        adapter.preflight()

    assert caught.value.code == "fault_host_systemd_version_invalid"
    assert len(executor.calls) == expected_calls
