"""配布するartifact・host・秘密値の境界。DB schemaはこの契約で変更しない。"""

from __future__ import annotations

from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretBytes, SecretStr, ValidationInfo, field_validator

from quiz_app.config import is_local_service_host, is_strong_aws_demo_secret
from quiz_app.load_test_aws.models import SHA256, StrictModel, utc_datetime

IMAGE = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")]
HOSTNAME = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]$")]


class RuntimeImages(StrictModel):
    prometheus: IMAGE
    postgres_exporter: IMAGE
    redis_exporter: IMAGE
    node_exporter: IMAGE
    toxiproxy: IMAGE

    @field_validator(
        "prometheus", "postgres_exporter", "redis_exporter", "node_exporter", "toxiproxy"
    )
    @classmethod
    def official_repository(cls, value: str, info: ValidationInfo) -> str:
        repositories = {
            "prometheus": {"docker.io/prom/prometheus", "quay.io/prometheus/prometheus"},
            "postgres_exporter": {"quay.io/prometheuscommunity/postgres-exporter"},
            "redis_exporter": {
                "docker.io/oliver006/redis_exporter",
                "ghcr.io/oliver006/redis_exporter",
                "quay.io/oliver006/redis_exporter",
            },
            "node_exporter": {"docker.io/prom/node-exporter", "quay.io/prometheus/node-exporter"},
            "toxiproxy": {"ghcr.io/shopify/toxiproxy"},
        }[info.field_name]
        if value.split("@", 1)[0] not in repositories:
            raise ValueError("aws_load_test_monitor_image_repository_invalid")
        return value


class HostTopology(StrictModel):
    app_hostname: HOSTNAME
    app_private_ip: IPv4Address
    runner_private_ip: IPv4Address
    redis_private_ip: IPv4Address
    database_endpoint: HOSTNAME
    redis_endpoint: HOSTNAME

    @field_validator("app_hostname", "database_endpoint", "redis_endpoint")
    @classmethod
    def remote_dns_name(cls, value: str) -> str:
        labels = value.split(".")
        if (
            is_local_service_host(value)
            or len(labels) < 2
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in labels
            )
        ):
            raise ValueError("aws_load_test_remote_dns_name_required")
        return value

    @field_validator("app_private_ip", "runner_private_ip", "redis_private_ip")
    @classmethod
    def private_address(cls, value: IPv4Address) -> IPv4Address:
        if not any(
            value in IPv4Network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise ValueError("aws_load_test_private_address_required")
        return value


class RuntimeDependencies(StrictModel):
    images: RuntimeImages
    postgres_ca_sha256: SHA256
    node_version: Annotated[str, Field(pattern=r"^22\.[0-9]+\.[0-9]+$")]
    node_archive_sha256: SHA256
    uv_archive_sha256: SHA256
    docker_compose_package_version: Annotated[
        str, Field(pattern=r"^2\.[0-9]+\.[0-9]+-1~ubuntu\.24\.04~noble$")
    ]
    docker_package_version: Annotated[
        str, Field(pattern=r"^5:(?:28|29)\.[0-9]+\.[0-9]+-1~ubuntu\.24\.04~noble$")
    ]

    @field_validator("docker_compose_package_version")
    @classmethod
    def supported_compose(cls, value: str) -> str:
        version = tuple(int(part) for part in value.split("-", 1)[0].split("."))
        if version < (2, 30, 0):
            raise ValueError("aws_load_test_compose_version_unsupported")
        return value


class DeploymentSettings(RuntimeDependencies):
    topology: HostTopology


class RuntimeSecrets(StrictModel):
    application_database_password: SecretStr
    verifier_database_password: SecretStr
    postgres_exporter_password: SecretStr
    redis_application_password: SecretStr
    redis_monitor_password: SecretStr
    session_secret: SecretStr
    backup_encryption_key: SecretStr
    operator_password: SecretStr

    @field_validator("redis_application_password", "redis_monitor_password")
    @classmethod
    def strong_redis_password(cls, value: SecretStr) -> SecretStr:
        if not is_strong_aws_demo_secret(value.get_secret_value(), minimum_length=16):
            raise ValueError("aws_load_test_redis_password_invalid")
        return value


class DeploymentFile(StrictModel):
    path: str
    body: SecretBytes
    mode: Literal[0o600, 0o644, 0o755] = 0o600

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or str(path) != value or ".." in path.parts or "\\" in value:
            raise ValueError("aws_load_test_archive_path_invalid")
        if value in {"", "."} or len(value) > 250 or any(c.isspace() for c in value):
            raise ValueError("aws_load_test_archive_path_invalid")
        return value


class TlsMaterial(StrictModel):
    ca_certificate: SecretBytes
    server_certificate: SecretBytes
    server_key: SecretBytes
    expires_at: datetime


class RuntimePayload(StrictModel):
    app: tuple[DeploymentFile, ...]
    runner: tuple[DeploymentFile, ...]
    human_database_sql: SecretBytes
    tls: TlsMaterial


class HostDeployment(StrictModel):
    format: Literal["load-test-host-deployment-v1"] = "load-test-host-deployment-v1"
    run_id: UUID
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    role: Literal["app", "runner"]
    manifest_sha256: SHA256
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    expires_at: datetime
    settings: DeploymentSettings

    _validate_time = field_validator("expires_at")(utc_datetime)


class HumanSchemaConfirmation(StrictModel):
    format: Literal["load-test-human-schema-confirmation-v1"]
    run_id: UUID
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    schema_revision: str = Field(min_length=1, max_length=64)
    schema_and_roles_applied_by_human: Literal[True]
    confirmed_at: datetime

    _validate_time = field_validator("confirmed_at")(utc_datetime)
