"""実環境へ接続せず、AWS rehearsalの合成後の2 API構成を検証する。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).parents[1]
API_IMAGE = f"registry.example/quiz/api@sha256:{'1' * 64}"


class ComposeDependency(BaseModel):
    condition: str


class ComposeHealthcheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    test: list[str]


class ComposePort(BaseModel):
    host_ip: str
    published: str
    target: int


class ComposeService(BaseModel):
    # 全属性を比較し、secondaryだけで欠落する安全設定やmountも検出する。
    model_config = ConfigDict(extra="allow")

    image: str
    environment: dict[str, str] = Field(default_factory=dict)
    depends_on: dict[str, ComposeDependency] = Field(default_factory=dict)
    healthcheck: ComposeHealthcheck | None = None
    command: list[str] | None = None
    ports: list[ComposePort] = Field(default_factory=list)


class ComposeConfiguration(BaseModel):
    services: dict[str, ComposeService]


def _configuration(
    tmp_path: Path, *, public: bool, monitoring: bool = True
) -> ComposeConfiguration:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is required for configuration validation")
    runtime = tmp_path / "runtime.env"
    runtime.write_text(
        "DATABASE_URL=postgresql+psycopg://fixture:fixture@db.example/quiz\n"
        "REDIS_URL=rediss://fixture:fixture@redis.example:6379/0\n"
        "SESSION_SECRET=configuration-only-session\n"
        "ROUND_BACKUP_ENCRYPTION_KEY=configuration-only-backup\n"
        "SESSION_COOKIE_SECURE=true\n"
        'REALTIME_ALLOWED_ORIGINS=["https://review.example"]\n',
        encoding="utf-8",
    )
    command = [docker, "compose", "--env-file", os.devnull, "--profile", "preflight"]
    for name in (
        "compose.yaml",
        "compose.rehearsal.yaml",
        *(("compose.rehearsal-monitoring.yaml",) if monitoring else ()),
        *(("compose.rehearsal-public.yaml",) if public else ()),
    ):
        command.extend(["-f", str(ROOT / "deploy/aws-demo" / name)])
    command.extend(["config", "--format", "json"])
    # daemonやAWSへ接続せず、専用fixtureの環境変数とenv fileだけを合成する。
    result = subprocess.run(  # noqa: S603 -- 固定Compose fileをshellなしで検証する。
        command,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "AWS_DEMO_API_IMAGE": API_IMAGE,
            "AWS_DEMO_GATEWAY_IMAGE": f"registry.example/quiz/gateway@sha256:{'2' * 64}",
            "AWS_DEMO_RUNTIME_ENV_FILE": str(runtime),
            "AWS_DEMO_POSTGRES_CA_FILE": str(tmp_path / "postgres-ca.pem"),
            "AWS_DEMO_GATEWAY_PORT": "18080",
            "AWS_DEMO_EVENT_ID": "60000000-0000-4000-8000-000000000002",
            "AWS_DEMO_ROUND_SCOPE": "rehearsal-six-round",
            "AWS_DEMO_RUNTIME_UID": "1001",
            "AWS_DEMO_RUNTIME_GID": "1001",
            "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY": str(tmp_path / "backups"),
            "REHEARSAL_PRIVATE_IPV4": "10.20.1.2",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return ComposeConfiguration.model_validate_json(result.stdout)


@pytest.mark.parametrize("public", [False, True])
def test_rehearsal_apis_share_the_entire_configuration_except_instance_id(
    tmp_path: Path, public: bool
) -> None:
    configuration = _configuration(tmp_path, public=public)
    assert set(configuration.services) == {"api", "api-secondary", "gateway", "preflight"}
    primary = configuration.services["api"]
    secondary = configuration.services["api-secondary"]
    assert primary.environment["API_INSTANCE_ID"] == "api-rehearsal"
    assert secondary.environment["API_INSTANCE_ID"] == "api-rehearsal-secondary"
    assert primary.image == secondary.image == API_IMAGE
    assert primary == secondary.model_copy(
        update={
            "environment": {**secondary.environment, "API_INSTANCE_ID": "api-rehearsal"},
            "ports": primary.ports,
        }
    )
    assert primary.environment["AWS_DEMO_ROUND_SCOPE"] == "rehearsal-six-round"
    assert primary.environment["SESSION_COOKIE_SECURE"] == "true"
    assert primary.environment["REDIS_URL"].startswith("rediss://")
    assert primary.environment["SESSION_SECRET"]  # 実秘密値を参照せず、fixtureの継承を確認する。
    assert primary.environment["ROUND_BACKUP_ENCRYPTION_KEY"] == "configuration-only-backup"
    assert primary.healthcheck is not None
    assert "/api/health/ready" in primary.healthcheck.test[-1]
    assert primary.ports == [ComposePort(host_ip="127.0.0.1", published="18001", target=8000)]
    assert secondary.ports == [ComposePort(host_ip="127.0.0.1", published="18002", target=8000)]
    for service in (primary, secondary):
        assert not any(key.startswith("SYSTEM_HEALTH_") for key in service.environment)
        assert service.model_extra is not None
        mounts = service.model_extra["volumes"]
        assert not any(mount["target"] == "/var/lib/quiz-monitoring" for mount in mounts)

    gateway = configuration.services["gateway"]
    assert gateway.depends_on["api"].condition == "service_healthy"
    assert gateway.depends_on["api-secondary"].condition == "service_healthy"
    assert gateway.command == [
        "run",
        "--config",
        "/etc/caddy/Caddyfile.multi-api",
        "--adapter",
        "caddyfile",
    ]
    assert {port.host_ip for port in gateway.ports} == (
        {"127.0.0.1", "10.20.1.2"} if public else {"127.0.0.1"}
    )


def test_manual_rehearsal_without_monitoring_has_no_probe_ports_or_snapshot_mount(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path, public=False, monitoring=False)
    for name in ("api", "api-secondary"):
        service = configuration.services[name]
        assert service.ports == []
        assert "SYSTEM_HEALTH_SNAPSHOT_DIRECTORY" not in service.environment
