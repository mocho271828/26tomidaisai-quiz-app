from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from quiz_app.load_test import fault_docker_runtime
from quiz_app.load_test.fault_host_config import FaultControllerHostError


def _directory_status(
    *,
    mode: int = 0o755,
    uid: int = 0,
    gid: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_gid=gid,
        st_dev=1,
        st_ino=2,
    )


def _stub_root_owned_status(
    monkeypatch: pytest.MonkeyPatch,
    status: SimpleNamespace,
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: status)
    monkeypatch.setattr(Path, "resolve", lambda path, *, strict: path)
    monkeypatch.setattr(os, "fstat", lambda _descriptor: status)


def test_root_owned_empty_docker_config_directory_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "docker-cli"
    directory.mkdir(mode=0o755)
    monkeypatch.setattr(fault_docker_runtime, "DOCKER_CONFIG_DIRECTORY", directory)
    _stub_root_owned_status(monkeypatch, _directory_status())

    assert fault_docker_runtime.require_root_owned_docker_config_directory() == directory


@pytest.mark.parametrize(
    "status",
    [
        _directory_status(mode=0o775),
        _directory_status(uid=1000),
        _directory_status(gid=1000),
        SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o755,
            st_uid=0,
            st_gid=0,
            st_dev=1,
            st_ino=2,
        ),
    ],
)
def test_docker_config_rejects_mutable_or_non_directory_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: SimpleNamespace,
) -> None:
    directory = tmp_path / "docker-cli"
    directory.mkdir(mode=0o755)
    monkeypatch.setattr(fault_docker_runtime, "DOCKER_CONFIG_DIRECTORY", directory)
    _stub_root_owned_status(monkeypatch, status)

    with pytest.raises(FaultControllerHostError, match="fault_host_docker_config_invalid"):
        fault_docker_runtime.require_root_owned_docker_config_directory()


def test_docker_config_rejects_any_cli_configuration_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "docker-cli"
    directory.mkdir(mode=0o755)
    (directory / "config.json").write_text("{}")
    monkeypatch.setattr(fault_docker_runtime, "DOCKER_CONFIG_DIRECTORY", directory)
    _stub_root_owned_status(monkeypatch, _directory_status())

    with pytest.raises(FaultControllerHostError, match="fault_host_docker_config_invalid"):
        fault_docker_runtime.require_root_owned_docker_config_directory()
