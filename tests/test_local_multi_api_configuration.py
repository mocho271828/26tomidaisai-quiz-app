"""Compose合成後も2つのAPIが同じDB・session・TLS・backup境界を使うことを確認する。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).parents[1]


class ComposeMount(BaseModel):
    type: str
    source: str
    target: str
    read_only: bool = False


class ComposeDependency(BaseModel):
    condition: str


class ComposeService(BaseModel):
    # 比較対象外のCompose属性も残し、片側だけのmount/起動引数変更を検出する。
    model_config = ConfigDict(extra="allow")

    image: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    volumes: list[ComposeMount] = Field(default_factory=list)
    depends_on: dict[str, ComposeDependency] = Field(default_factory=dict)


class ComposeConfiguration(BaseModel):
    services: dict[str, ComposeService]


def _compose_configuration(
    *overrides: str, project_name: str | None = None
) -> ComposeConfiguration:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is required for configuration validation")
    command = [docker, "compose", "--env-file", os.devnull, "-f", "compose.yaml"]
    if project_name is not None:
        command.extend(["--project-name", project_name])
    for override in overrides:
        command.extend(["-f", override])
    command.extend(["config", "--format", "json"])
    # 実DBや秘密値を参照せず、daemon接続を伴わないconfigだけを実行する。
    result = subprocess.run(  # noqa: S603 -- 固定Compose fileだけをshellなしで検証する。
        command,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "POSTGRES_PASSWORD": "configuration-only-database-password",
            "ROUND_BACKUP_ENCRYPTION_KEY": os.environ["ROUND_BACKUP_ENCRYPTION_KEY"],
            "LOCAL_TLS_REDIS_PASSWORD": "configuration-only-redis-password",
            "LOCAL_TLS_ALLOWED_ORIGINS": '["https://localhost:18443"]',
            "LOCAL_TLS_CERT_DIRECTORY": "/configuration-only/certificates",
            "LOCAL_TLS_POSTGRES_VOLUME": "configuration-only-postgres",
            "LOCAL_TLS_PORT": "18443",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return ComposeConfiguration.model_validate_json(result.stdout)


@pytest.mark.parametrize("tls", [False, True])
def test_secondary_inherits_the_entire_primary_configuration_except_instance_id(tls: bool) -> None:
    overrides = ["compose.local-multi-api.yaml"]
    if tls:
        overrides.extend(["compose.local-tls.yaml", "compose.local-tls-multi-api.yaml"])
    configuration = _compose_configuration(*overrides)
    primary = configuration.services["api"]
    secondary = configuration.services["api-secondary"]
    assert primary.environment["API_INSTANCE_ID"] == "api-local-primary"
    assert secondary.environment["API_INSTANCE_ID"] == "api-local-secondary"
    assert primary.image == secondary.image == "gs-app-tomidaisai-api"
    assert primary == secondary.model_copy(
        update={"environment": {**secondary.environment, "API_INSTANCE_ID": "api-local-primary"}}
    )
    if tls:
        assert secondary.environment["PGSSLMODE"] == "verify-full"
        assert secondary.environment["REDIS_URL"].startswith("rediss://")
        assert secondary.environment["SESSION_COOKIE_SECURE"] == "true"
        assert secondary.environment["REALTIME_ALLOWED_ORIGINS"] == '["https://localhost:18443"]'
    else:
        assert secondary.environment["SESSION_COOKIE_SECURE"] == "false"
    gateway = configuration.services["gateway"]
    expected_source = (
        "infra/local-tls/Caddyfile.multi-api" if tls else "infra/Caddyfile.local-multi-api"
    )
    assert (
        ComposeMount(
            type="bind",
            source=str(ROOT / expected_source),
            target="/etc/caddy/Caddyfile",
            read_only=True,
        )
        in gateway.volumes
    )
    assert gateway.depends_on["api"].condition == "service_healthy"
    assert gateway.depends_on["api-secondary"].condition == "service_healthy"


@pytest.mark.parametrize("project_name", ["gs-app-tomidaisai-tls", "configuration-only-secondary"])
def test_api_image_is_shared_inside_each_project_and_isolated_between_projects(
    project_name: str,
) -> None:
    configuration = _compose_configuration(
        "compose.local-multi-api.yaml", project_name=project_name
    )
    assert (
        configuration.services["api"].image
        == configuration.services["api-secondary"].image
        == f"{project_name}-api"
    )


@pytest.mark.parametrize(
    "configuration", ["infra/Caddyfile.local-multi-api", "infra/local-tls/Caddyfile.multi-api"]
)
def test_two_api_gateway_keeps_recovery_available_without_replaying_mutations(
    configuration: str,
) -> None:
    source = (ROOT / configuration).read_text()
    assert "reverse_proxy api:8000 api-secondary:8000" in source
    assert "lb_policy least_conn" in source
    assert "health_uri /api/health/live" in source
    assert "health_uri /api/health/ready" not in source
    assert "lb_try_duration 1s" in source
    assert "lb_retry_match method GET" in source
    assert "unhealthy_status" not in source
    assert "lb_policy cookie" not in source
    assert "@internal path /internal /internal/*" in source
