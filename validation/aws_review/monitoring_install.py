"""root の固定手順で、DB 秘密値を持たない専用監視サービスを配置する。"""

import argparse
import grp
import os
import pwd
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError

from quiz_app.system_monitoring.collector_config import (
    STATE_DIRECTORY,
    CollectorConfig,
    read_collector_config,
)
from validation.aws_review.diagnostics import configure_logging, log_failure
from validation.aws_review.host_contracts import HostInput

ROOT = Path("/opt/gs-app-tomidaisai")
CONFIG_DIRECTORY = Path("/etc/gs-app-tomidaisai-monitoring")
CONFIG_FILE = CONFIG_DIRECTORY / "config.json"
SERVICE_FILE = Path("/etc/systemd/system/gs-quiz-monitor.service")
SERVICE = """[Unit]
Description=Quiz CloudWatch metrics publisher
After=network-online.target docker.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=quiz-monitor
Group=ubuntu
WorkingDirectory=/opt/gs-app-tomidaisai/backend
ExecStart=/opt/gs-app-tomidaisai/backend/.venv/bin/python \\
  -m quiz_app.system_monitoring.collector \\
  --config /etc/gs-app-tomidaisai-monitoring/config.json
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=30
TimeoutStopSec=10
UMask=0027
CPUQuota=10%
MemoryMax=256M
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/quiz-monitoring

[Install]
WantedBy=multi-user.target
"""


class _RetiredCollectorConfig(CollectorConfig):
    """既存hostの期限を維持するため、配置時に限り旧形式を厳密に読み取る。"""

    instance_id: str = Field(pattern=r"^i-[a-f0-9]{17}$")
    database_identifier: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{0,62}$")
    redis_cluster_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,49}$")
    redis_node_id: str = Field(default="0001", pattern=r"^[0-9]{4}$")
    distribution_id: str = Field(pattern=r"^[A-Z0-9]{1,32}$")
    cpu_period_seconds: Literal[60, 300] = 60


def _previous_config(path: Path) -> CollectorConfig:
    previous = TypeAdapter(CollectorConfig | _RetiredCollectorConfig).validate_json(
        read_collector_config(path)
    )
    return CollectorConfig(
        region=previous.region,
        environment_id=previous.environment_id,
        expires_at=previous.expires_at,
    )


def collector_config(
    inputs: HostInput, previous: CollectorConfig | None, *, now: datetime
) -> CollectorConfig:
    """update の未指定期限は維持し、期限切れを暗黙に延長しない。"""
    environment_id = f"gs-rehearsal-{inputs.demo_id}"
    if previous is not None and previous.environment_id != environment_id:
        raise ValueError("review_monitoring_environment_mismatch")
    expiry = inputs.monitoring_expires_at
    if expiry is None:
        if inputs.deployment_mode != "update" or previous is None:
            raise ValueError("review_monitoring_expiry_required")
        expiry = previous.expires_at
    elif not now < expiry <= now + timedelta(hours=12):
        raise ValueError("review_monitoring_expiry_must_be_within_12_hours")
    return CollectorConfig(
        environment_id=environment_id,
        expires_at=expiry,
    )


def _directory(path: Path, *, uid: int, gid: int) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.resolve() != path:
            raise ValueError("review_monitoring_directory_unsafe")
    else:
        path.mkdir(mode=0o750)
    os.chown(path, uid, gid)
    path.chmod(0o750)


def _write(path: Path, value: str, *, gid: int, mode: int) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise ValueError("review_monitoring_file_unsafe")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, gid)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_monitor_user() -> pwd.struct_passwd:
    try:
        user = pwd.getpwnam("quiz-monitor")
    except KeyError:
        subprocess.run(  # noqa: S603 -- 引数は固定し、docker group には加えない。
            [
                "/usr/sbin/useradd",
                "--system",
                "--gid",
                "ubuntu",
                "--home-dir",
                "/nonexistent",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "quiz-monitor",
            ],
            check=True,
            timeout=30,
        )
        user = pwd.getpwnam("quiz-monitor")
    groups = os.getgrouplist(user.pw_name, user.pw_gid)
    if (
        user.pw_uid in (0, 1000)
        or user.pw_gid != 1000
        or user.pw_shell != "/usr/sbin/nologin"
        or user.pw_dir != "/nonexistent"
        or groups != [1000]
    ):
        raise ValueError("review_monitoring_user_unsafe")
    return user


def _allow_backend_read() -> None:
    """ProtectHome 下で実行できる system Python と、必要な source だけを許可する。"""
    if (ROOT / "backend/.venv/bin/python").resolve() != Path("/usr/bin/python3").resolve():
        raise ValueError("review_monitoring_system_python_required")
    # Logfire は起動時に作業ディレクトリの設定を読むため、この非秘密fileだけを追加する。
    project_configuration = ROOT / "backend/pyproject.toml"
    paths = [ROOT, ROOT / "backend", project_configuration]
    for tree in (ROOT / "backend/src", ROOT / "backend/.venv"):
        paths.append(tree)
        paths.extend(tree.rglob("*"))
    for path in paths:
        info = path.lstat()
        if path == project_configuration and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("review_monitoring_source_unsafe")
        if stat.S_ISLNK(info.st_mode):
            continue
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or info.st_uid != 1000:
            raise ValueError("review_monitoring_source_unsafe")
        os.chown(path, 1000, 1000)
        path.chmod(
            stat.S_IMODE(info.st_mode) | stat.S_IRGRP | (stat.S_IXGRP if path.is_dir() else 0)
        )


def install(inputs: HostInput, *, now: datetime | None = None) -> None:
    """root 以外の実行を拒否し、設定だけを配置する。サービスの起動は host 成功後。"""
    if (
        os.geteuid() != 0
        or pwd.getpwnam("ubuntu").pw_uid != 1000
        or grp.getgrnam("ubuntu").gr_gid != 1000
    ):
        raise ValueError("review_monitoring_root_required")
    previous = None
    if CONFIG_FILE.exists() or CONFIG_FILE.is_symlink():
        if CONFIG_FILE.lstat().st_uid != 0:
            raise ValueError("review_monitoring_config_owner_invalid")
        previous = _previous_config(CONFIG_FILE)
    config = collector_config(inputs, previous, now=now or datetime.now(UTC))
    user = _require_monitor_user()
    _allow_backend_read()
    _directory(CONFIG_DIRECTORY, uid=0, gid=1000)
    _directory(STATE_DIRECTORY, uid=user.pw_uid, gid=1000)
    _write(CONFIG_FILE, config.model_dump_json(indent=2) + "\n", gid=1000, mode=0o640)
    _write(SERVICE_FILE, SERVICE, gid=0, mode=0o644)


def _load_inputs(path: Path) -> HostInput:
    if path != Path("/etc/gs-app-tomidaisai/review-input.json") or path.resolve() != path:
        raise ValueError("review_monitoring_input_path_invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 1000
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16_384
        ):
            raise ValueError("review_monitoring_input_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            inputs = HostInput.model_validate_json(stream.read(16_385))
    finally:
        os.close(descriptor)
    return inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the dedicated quiz monitoring service")
    parser.add_argument("--release-input", type=Path, required=True)
    arguments = parser.parse_args(argv)
    configure_logging()
    try:
        install(_load_inputs(arguments.release_input))
    except (OSError, ValueError, KeyError, ValidationError, subprocess.SubprocessError) as error:
        log_failure("review_host_failed", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
