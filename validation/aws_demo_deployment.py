from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeVar
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url

from quiz_app.aws_demo_paths import (
    LOAD_TEST_MANIFEST_FILE_POLICY,
    POSTGRES_CA_FILE_POLICY,
    POSTGRES_EXPORTER_PASSWORD_FILE_POLICY,
    REDIS_EXPORTER_PASSWORD_FILE_POLICY,
    RELEASE_MANIFEST_FILE_POLICY,
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    ValidatedExternalPath,
    read_validated_external_file,
    validate_external_artifact_directory,
    validate_external_regular_file,
    validate_round_backup_directory,
)
from quiz_app.config import AppEnvironment, Settings, is_local_service_host
from quiz_app.load_test.fault_topology import validate_external_fault_topology
from quiz_app.load_test.manifest import MAX_MANIFEST_BYTES, LoadTestManifest
from quiz_app.schema_revision import SCHEMA_REVISION

ENV_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")
IMAGE_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
)
SOURCE_COMMIT_PATTERN: Final[str] = r"^[0-9a-f]{40}$"
RELEASE_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9._-]{2,63}$"
DOCKER_CONTEXT_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
EXECUTION_HOST_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]$"
SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
EXPORTER_USERNAME_PATTERN: Final[str] = r"^[a-z][a-z0-9_]{2,31}$"
LOCAL_DEMO_EVENT_ID: Final[UUID] = UUID("60000000-0000-4000-8000-000000000001")
AWS_DEMO_POSTGRES_CA_CONTAINER_PATH: Final[str] = "/run/secrets/aws-demo-postgres-ca.pem"
AWS_DEMO_ROUND_BACKUP_CONTAINER_PATH: Final[str] = "/var/lib/quiz-round-backups"
MAXIMUM_CA_BUNDLE_BYTES: Final[int] = 1_048_576
MAXIMUM_EXPORTER_PASSWORD_BYTES: Final[int] = 512
MAXIMUM_EXPORTER_CREDENTIAL_FILE_BYTES: Final[int] = 2_048
PROMETHEUS_CONFIG_RELATIVE_PATH: Final[Path] = Path("infra/prometheus.load-test.yml")
APPROVED_LOAD_SUPPORT_IMAGE_REPOSITORIES: Final[dict[str, frozenset[str]]] = {
    "prometheus_image": frozenset({"docker.io/prom/prometheus", "quay.io/prometheus/prometheus"}),
    "postgres_exporter_image": frozenset({"quay.io/prometheuscommunity/postgres-exporter"}),
    "redis_exporter_image": frozenset(
        {
            "docker.io/oliver006/redis_exporter",
            "ghcr.io/oliver006/redis_exporter",
            "quay.io/oliver006/redis_exporter",
        }
    ),
    "node_exporter_image": frozenset(
        {"docker.io/prom/node-exporter", "quay.io/prometheus/node-exporter"}
    ),
    "toxiproxy_image": frozenset({"ghcr.io/shopify/toxiproxy"}),
}
APPROVED_TOXIPROXY_IMAGES: Final[frozenset[str]] = frozenset(
    {
        "ghcr.io/shopify/toxiproxy@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e",
        "ghcr.io/shopify/toxiproxy@sha256:a3e244375123dad8849091bcc59775e188624d3f602db01901f9af855682fef8",
        "ghcr.io/shopify/toxiproxy@sha256:5ab4b4e8f476fd5452eb9584a608dd7cf8c11135878a8f7953722a9fcb9b3d87",
    }
)
ObservabilityValue = TypeVar("ObservabilityValue")


def _decode_url_username(username: str | None) -> str | None:
    """URL userinfoを厳密にdecodeし、ACL identity比較の抜け道を防ぐ。"""

    if username is None:
        return None
    if re.search(r"%(?![0-9A-Fa-f]{2})", username):
        raise AwsDemoDeploymentValidationError("redis_application_user_invalid")
    try:
        return unquote(username, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AwsDemoDeploymentValidationError("redis_application_user_invalid") from error


RUNTIME_ENVIRONMENT_FIELDS: Final[dict[str, str]] = {
    "APP_ENV": "app_env",
    "APP_VERSION": "app_version",
    "DATABASE_URL": "database_url",
    "REDIS_URL": "redis_url",
    "DEMO_OPERATOR_USERNAME": "demo_operator_username",
    "DEMO_OPERATOR_PASSWORD": "demo_operator_password",
    "SESSION_SECRET": "session_secret",
    "ENABLE_DEMO_AUTH": "enable_demo_auth",
    "ENABLE_DEMO_SEED": "enable_demo_seed",
    "SESSION_COOKIE_SECURE": "session_cookie_secure",
    "OPERATOR_SESSION_TTL_MINUTES": "operator_session_ttl_minutes",
    "PARTICIPANT_SESSION_TTL_MINUTES": "participant_session_ttl_minutes",
    "PARTICIPANT_JOIN_RETRY_MINUTES": "participant_join_retry_minutes",
    "REALTIME_ALLOWED_ORIGINS": "realtime_allowed_origins",
    "REDIS_CONNECT_TIMEOUT_SECONDS": "redis_connect_timeout_seconds",
    "REDIS_OPERATION_TIMEOUT_SECONDS": "redis_operation_timeout_seconds",
    "PGSSLROOTCERT": "postgres_ssl_root_cert",
    "AWS_DEMO_ROUND_SCOPE": "aws_demo_round_scope",
    "API_INSTANCE_ID": "api_instance_id",
    "LOGFIRE_SEND_TO_LOGFIRE": "logfire_send_to_logfire",
    "ROUND_BACKUP_STORAGE_MODE": "round_backup_storage_mode",
    "ROUND_BACKUP_DIRECTORY": "round_backup_directory",
    "ROUND_BACKUP_ENCRYPTION_KEY": "round_backup_encryption_key",
}

RELEASE_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "AWS_DEMO_RELEASE_ID",
        "AWS_DEMO_SOURCE_COMMIT",
        "AWS_DEMO_API_IMAGE",
        "AWS_DEMO_GATEWAY_IMAGE",
        "AWS_DEMO_RUNTIME_ENV_FILE",
        "AWS_DEMO_POSTGRES_CA_FILE",
        "AWS_DEMO_POSTGRES_CA_SHA256",
        "AWS_DEMO_EXECUTION_HOST",
        "AWS_DEMO_DOCKER_CONTEXT",
        "AWS_DEMO_DOCKER_ENDPOINT",
        "AWS_DEMO_RUNTIME_UID",
        "AWS_DEMO_RUNTIME_GID",
        "AWS_DEMO_EVENT_ID",
        "AWS_DEMO_ROUND_SCOPE",
        "AWS_DEMO_SCHEMA_REVISION",
        "AWS_DEMO_GATEWAY_PORT",
    }
)
OPTIONAL_RELEASE_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY",
        "AWS_DEMO_PROMETHEUS_IMAGE",
        "AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
        "AWS_DEMO_REDIS_EXPORTER_IMAGE",
        "AWS_DEMO_NODE_EXPORTER_IMAGE",
        "AWS_DEMO_PROMETHEUS_CONFIG_SHA256",
        "AWS_DEMO_POSTGRES_EXPORTER_URI",
        "AWS_DEMO_POSTGRES_EXPORTER_USER",
        "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
        "AWS_DEMO_REDIS_EXPORTER_ADDR",
        "AWS_DEMO_REDIS_EXPORTER_USER",
        "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
        "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
        "AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
        "AWS_DEMO_TOXIPROXY_IMAGE",
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256",
        "AWS_DEMO_REDIS_HOSTNAME",
    }
)

SAFE_OUTPUT_FIELDS: Final[dict[str, str]] = {
    "release-id": "release_id",
    "source-commit": "source_commit",
    "api-image": "api_image",
    "gateway-image": "gateway_image",
    "prometheus-image": "prometheus_image",
    "postgres-exporter-image": "postgres_exporter_image",
    "redis-exporter-image": "redis_exporter_image",
    "node-exporter-image": "node_exporter_image",
    "toxiproxy-image": "toxiproxy_image",
    "execution-host": "execution_host",
    "docker-context": "docker_context",
    "docker-endpoint": "docker_endpoint",
    "round-scope": "round_scope",
    "schema-revision": "schema_revision",
    "gateway-port": "gateway_port",
}
REALTIME_ORIGINS_ADAPTER: Final[TypeAdapter[list[str]]] = TypeAdapter(list[str])

SECRET_ENVIRONMENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"DATABASE_URL", "REDIS_URL", "SESSION_SECRET", "ROUND_BACKUP_ENCRYPTION_KEY"}
)
KNOWN_PLACEHOLDER_FRAGMENTS: Final[tuple[str, ...]] = (
    "<required",
    "__required",
    "change-me",
    "local-demo-only",
    "local-demo-session-secret",
    "replace-for-local-review",
    "local-only-database-password",
)


class AwsDemoReleaseManifest(BaseModel):
    """秘密値を持たないimmutable release記録。"""

    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(alias="AWS_DEMO_RELEASE_ID", pattern=RELEASE_ID_PATTERN)
    source_commit: str = Field(alias="AWS_DEMO_SOURCE_COMMIT", pattern=SOURCE_COMMIT_PATTERN)
    api_image: str = Field(alias="AWS_DEMO_API_IMAGE")
    gateway_image: str = Field(alias="AWS_DEMO_GATEWAY_IMAGE")
    runtime_env_file: Path = Field(alias="AWS_DEMO_RUNTIME_ENV_FILE")
    postgres_ca_file: Path = Field(alias="AWS_DEMO_POSTGRES_CA_FILE")
    postgres_ca_sha256: str = Field(
        alias="AWS_DEMO_POSTGRES_CA_SHA256",
        pattern=SHA256_PATTERN,
    )
    execution_host: str = Field(
        alias="AWS_DEMO_EXECUTION_HOST",
        pattern=EXECUTION_HOST_PATTERN,
    )
    docker_context: str = Field(
        alias="AWS_DEMO_DOCKER_CONTEXT",
        pattern=DOCKER_CONTEXT_PATTERN,
    )
    docker_endpoint: Literal["unix:///var/run/docker.sock"] = Field(
        alias="AWS_DEMO_DOCKER_ENDPOINT"
    )
    runtime_uid: int = Field(alias="AWS_DEMO_RUNTIME_UID", ge=1, le=2_147_483_647)
    runtime_gid: int = Field(alias="AWS_DEMO_RUNTIME_GID", ge=1, le=2_147_483_647)
    event_id: UUID = Field(alias="AWS_DEMO_EVENT_ID")
    round_scope: Literal["single-round", "rehearsal-six-round", "load-test-ten-round"] = Field(
        alias="AWS_DEMO_ROUND_SCOPE"
    )
    round_backup_host_directory: Path | None = Field(
        default=None,
        alias="AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY",
    )
    prometheus_image: str | None = Field(default=None, alias="AWS_DEMO_PROMETHEUS_IMAGE")
    postgres_exporter_image: str | None = Field(
        default=None,
        alias="AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
    )
    redis_exporter_image: str | None = Field(
        default=None,
        alias="AWS_DEMO_REDIS_EXPORTER_IMAGE",
    )
    node_exporter_image: str | None = Field(
        default=None,
        alias="AWS_DEMO_NODE_EXPORTER_IMAGE",
    )
    toxiproxy_image: str | None = Field(
        default=None,
        alias="AWS_DEMO_TOXIPROXY_IMAGE",
    )
    toxiproxy_config_file: Path | None = Field(
        default=None,
        alias="AWS_DEMO_TOXIPROXY_CONFIG_FILE",
    )
    toxiproxy_config_sha256: str | None = Field(
        default=None,
        alias="AWS_DEMO_TOXIPROXY_CONFIG_SHA256",
        pattern=SHA256_PATTERN,
    )
    redis_hostname: str | None = Field(
        default=None,
        alias="AWS_DEMO_REDIS_HOSTNAME",
        min_length=3,
        max_length=253,
    )
    prometheus_config_sha256: str | None = Field(
        default=None,
        alias="AWS_DEMO_PROMETHEUS_CONFIG_SHA256",
        pattern=SHA256_PATTERN,
    )
    postgres_exporter_uri: str | None = Field(
        default=None,
        alias="AWS_DEMO_POSTGRES_EXPORTER_URI",
        max_length=512,
    )
    postgres_exporter_user: str | None = Field(
        default=None,
        alias="AWS_DEMO_POSTGRES_EXPORTER_USER",
        pattern=EXPORTER_USERNAME_PATTERN,
    )
    postgres_exporter_password_file: Path | None = Field(
        default=None,
        alias="AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
    )
    redis_exporter_addr: str | None = Field(
        default=None,
        alias="AWS_DEMO_REDIS_EXPORTER_ADDR",
        max_length=512,
    )
    redis_exporter_user: str | None = Field(
        default=None,
        alias="AWS_DEMO_REDIS_EXPORTER_USER",
        pattern=EXPORTER_USERNAME_PATTERN,
    )
    redis_exporter_password_file: Path | None = Field(
        default=None,
        alias="AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
    )
    metrics_artifact_host_directory: Path | None = Field(
        default=None,
        alias="AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
    )
    load_test_manifest_file: Path | None = Field(
        default=None,
        alias="AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
    )
    schema_revision: str = Field(alias="AWS_DEMO_SCHEMA_REVISION")
    gateway_port: int = Field(alias="AWS_DEMO_GATEWAY_PORT", ge=1_024, le=65_535)

    @field_validator(
        "api_image",
        "gateway_image",
        "prometheus_image",
        "postgres_exporter_image",
        "redis_exporter_image",
        "node_exporter_image",
        "toxiproxy_image",
    )
    @classmethod
    def validate_immutable_image_reference(cls, image: str | None) -> str | None:
        """tagや見本digestではなく、取得済みimageのdigest参照だけを許可する。"""

        if image is None:
            return None
        if IMAGE_REFERENCE_PATTERN.fullmatch(image) is None:
            raise ValueError("imageはsha256 digestで固定してください")
        digest = image.rsplit(":", maxsplit=1)[1]
        if len(set(digest)) < 4:
            raise ValueError("見本用image digestは使用できません")
        return image

    @field_validator("toxiproxy_image")
    @classmethod
    def validate_approved_toxiproxy_image(cls, image: str | None) -> str | None:
        """公式v2.12.0のindexまたは対応platform manifestだけを許可する。"""

        if image is not None and image not in APPROVED_TOXIPROXY_IMAGES:
            raise ValueError("Toxiproxy imageは承認済みv2.12.0 digestへ固定してください")
        return image

    @model_validator(mode="after")
    def validate_load_support_image_repositories(self) -> AwsDemoReleaseManifest:
        """credential・通信経路を扱う補助imageを公式repositoryへ限定する。"""

        for (
            field_name,
            approved_repositories,
        ) in APPROVED_LOAD_SUPPORT_IMAGE_REPOSITORIES.items():
            image = getattr(self, field_name)
            if image is None:
                continue
            repository, _, _ = image.partition("@")
            if repository not in approved_repositories:
                raise ValueError(f"{field_name}は承認済み公式repositoryから指定してください")
        return self

    @field_validator("schema_revision")
    @classmethod
    def validate_schema_revision(cls, revision: str) -> str:
        """imageが期待するschema以外への配置を止める。"""

        if revision != SCHEMA_REVISION:
            raise ValueError("release schema revisionがapplicationと一致しません")
        return revision

    @field_validator("event_id")
    @classmethod
    def reject_local_event_id(cls, event_id: UUID) -> UUID:
        """seed済みlocal Event IDをAWS releaseへ流用させない。"""

        if event_id == LOCAL_DEMO_EVENT_ID:
            raise ValueError("local demo Event IDは使用できません")
        return event_id

    @field_validator("execution_host")
    @classmethod
    def validate_execution_host(cls, execution_host: str) -> str:
        """local・予約hostを配置先identityとして受理しない。"""

        if is_local_service_host(execution_host) or ".." in execution_host:
            raise ValueError("AWS demo execution hostには承認済みFQDNが必要です")
        if any(
            not label or label.startswith("-") or label.endswith("-")
            for label in execution_host.split(".")
        ):
            raise ValueError("AWS demo execution hostのlabelが不正です")
        return execution_host

    @model_validator(mode="after")
    def validate_topology_fields(self) -> AwsDemoReleaseManifest:
        """load scopeだけが明示されたhost backup directoryを持つよう固定する。"""

        observability_values = (
            self.prometheus_image,
            self.postgres_exporter_image,
            self.redis_exporter_image,
            self.node_exporter_image,
            self.prometheus_config_sha256,
            self.postgres_exporter_uri,
            self.postgres_exporter_user,
            self.postgres_exporter_password_file,
            self.redis_exporter_addr,
            self.redis_exporter_user,
            self.redis_exporter_password_file,
            self.metrics_artifact_host_directory,
            self.load_test_manifest_file,
            self.toxiproxy_image,
            self.toxiproxy_config_file,
            self.toxiproxy_config_sha256,
            self.redis_hostname,
        )
        if self.round_scope != "single-round":
            directory = self.round_backup_host_directory
            if directory is None or not directory.is_absolute() or directory == Path("/"):
                raise ValueError("複数問のAWS demoには絶対backup host directoryが必要です")
        elif self.round_backup_host_directory is not None:
            raise ValueError("single-roundにbackup host directoryを指定できません")
        if self.round_scope == "load-test-ten-round":
            if any(value is None for value in observability_values):
                raise ValueError("load-test-ten-roundには完全なobservability設定が必要です")
        elif any(value is not None for value in observability_values):
            raise ValueError("負荷試験以外にobservability設定を指定できません")
        return self


@dataclass(frozen=True, slots=True)
class ValidatedAwsDemoDeployment:
    release: AwsDemoReleaseManifest
    rollback: AwsDemoReleaseManifest | None
    release_settings: Settings
    rollback_settings: Settings | None


@dataclass(frozen=True, slots=True)
class ValidatedAwsDemoRollback:
    release: AwsDemoReleaseManifest
    failed_release: AwsDemoReleaseManifest
    release_settings: Settings


def parse_strict_env_file(
    path: Path,
    expected_fields: frozenset[str],
    *,
    optional_fields: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """shell/Compose解釈差を許さず、単純なKEY=VALUE形式だけを読み取る。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise AwsDemoDeploymentValidationError("environment_file_unreadable") from error

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line or raw_line.startswith("#"):
            continue
        key, separator, value = raw_line.partition("=")
        if (
            not separator
            or ENV_KEY_PATTERN.fullmatch(key) is None
            or value != value.strip()
            or not value.isascii()
            or any(character.isspace() for character in value)
            or value.startswith(("'", '"'))
            or any(character in value for character in ("$", "\\", "#", "\x00"))
            or key in values
        ):
            raise AwsDemoDeploymentValidationError(
                "environment_file_syntax_invalid",
                (f"line-{line_number}",),
            )
        values[key] = value

    actual_fields = frozenset(values)
    allowed_fields = expected_fields | optional_fields
    if not expected_fields.issubset(actual_fields) or not actual_fields.issubset(allowed_fields):
        invalid_fields = tuple(
            sorted((expected_fields - actual_fields) | (actual_fields - allowed_fields))
        )
        raise AwsDemoDeploymentValidationError("environment_fields_mismatch", invalid_fields)
    return values


def validate_runtime_env_file_permissions(path: Path, repository_root: Path) -> None:
    """secret fileをrepository、symlink、共有権限から隔離する。"""

    validate_external_regular_file(
        path,
        repository_root,
        RUNTIME_ENVIRONMENT_FILE_POLICY,
    )


def validate_release_manifest_file(path: Path, repository_root: Path) -> AwsDemoReleaseManifest:
    """repository外のrelease記録を厳格に検証する。"""

    validate_external_regular_file(path, repository_root, RELEASE_MANIFEST_FILE_POLICY)
    raw_values = parse_strict_env_file(
        path,
        RELEASE_MANIFEST_FIELDS,
        optional_fields=OPTIONAL_RELEASE_MANIFEST_FIELDS,
    )
    try:
        manifest = AwsDemoReleaseManifest.model_validate(raw_values)
    except ValidationError as error:
        fields = tuple(sorted(_validation_error_fields(error)))
        raise AwsDemoDeploymentValidationError("release_manifest_invalid", fields) from error
    return manifest


def validate_release_execution_identity(manifest: AwsDemoReleaseManifest) -> None:
    """Compose writer UID/GIDをpreflight実行accountへ固定する。"""

    if manifest.runtime_uid != os.geteuid() or manifest.runtime_gid != os.getegid():
        raise AwsDemoDeploymentValidationError("runtime_identity_mismatch")


def validate_round_backup_host_directory(
    manifest: AwsDemoReleaseManifest,
    repository_root: Path,
    *,
    protected_paths: tuple[Path, ...],
    optional_protected_paths: tuple[Path, ...] = (),
) -> None:
    """複数問のbackup bind元をowner限定のrepository外directoryへ固定する。"""

    if manifest.round_scope == "single-round":
        return
    directory = manifest.round_backup_host_directory
    if directory is None:
        raise AwsDemoDeploymentValidationError("round_backup_host_directory_required")
    validate_round_backup_directory(
        directory,
        repository_root,
        runtime_uid=manifest.runtime_uid,
        runtime_gid=manifest.runtime_gid,
        protected_paths=protected_paths,
        optional_protected_paths=optional_protected_paths,
    )


def validate_postgres_ca_file(
    manifest: AwsDemoReleaseManifest,
    repository_root: Path,
) -> None:
    """承認済みCA bundleをpathとchecksumで固定し、private key混入を拒否する。"""

    path = manifest.postgres_ca_file
    validate_external_regular_file(path, repository_root, POSTGRES_CA_FILE_POLICY)
    try:
        certificate_bundle = path.read_bytes()
    except OSError as error:
        raise AwsDemoDeploymentValidationError("postgres_ca_file_unavailable") from error
    if not 0 < len(certificate_bundle) <= MAXIMUM_CA_BUNDLE_BYTES:
        raise AwsDemoDeploymentValidationError("postgres_ca_size_invalid")
    if (
        b"-----BEGIN CERTIFICATE-----" not in certificate_bundle
        or b"-----END CERTIFICATE-----" not in certificate_bundle
        or b"PRIVATE KEY" in certificate_bundle
    ):
        raise AwsDemoDeploymentValidationError("postgres_ca_content_invalid")
    actual_digest = hashlib.sha256(certificate_bundle).hexdigest()
    if not hmac.compare_digest(actual_digest, manifest.postgres_ca_sha256):
        raise AwsDemoDeploymentValidationError("postgres_ca_checksum_mismatch")


def _require_observability_value(
    value: ObservabilityValue | None,
) -> ObservabilityValue:
    if value is None:
        raise AwsDemoDeploymentValidationError("observability_configuration_incomplete")
    return value


def _read_bounded_external_file(
    validated: ValidatedExternalPath,
    *,
    error_code: str,
    maximum_bytes: int = MAXIMUM_EXPORTER_CREDENTIAL_FILE_BYTES,
) -> bytes:
    return read_validated_external_file(
        validated,
        maximum_bytes=maximum_bytes,
        unavailable_code=error_code,
    )


def _read_exporter_password(
    validated: ValidatedExternalPath,
    *,
    error_code: str,
) -> str:
    raw_password = _read_bounded_external_file(validated, error_code=error_code)
    if not 16 <= len(raw_password) <= MAXIMUM_EXPORTER_PASSWORD_BYTES:
        raise AwsDemoDeploymentValidationError(error_code)
    try:
        password = raw_password.decode("ascii")
    except UnicodeDecodeError as error:
        raise AwsDemoDeploymentValidationError(error_code) from error
    if any(character.isspace() or ord(character) < 0x21 for character in password):
        raise AwsDemoDeploymentValidationError(error_code)
    if _contains_known_placeholder(password):
        raise AwsDemoDeploymentValidationError(error_code)
    return password


def validate_observability_configuration(
    manifest: AwsDemoReleaseManifest,
    settings: Settings,
    repository_root: Path,
) -> tuple[Path, ...]:
    """load-only監視設定をapplication接続先と秘密fileへ固定する。"""

    if manifest.round_scope != "load-test-ten-round":
        return ()

    config_digest = _require_observability_value(manifest.prometheus_config_sha256)
    config_path = repository_root / PROMETHEUS_CONFIG_RELATIVE_PATH
    try:
        config_body = config_path.read_bytes()
    except OSError as error:
        raise AwsDemoDeploymentValidationError("prometheus_config_unavailable") from error
    if not hmac.compare_digest(hashlib.sha256(config_body).hexdigest(), config_digest):
        raise AwsDemoDeploymentValidationError("prometheus_config_checksum_mismatch")

    postgres_password_path = _require_observability_value(manifest.postgres_exporter_password_file)
    redis_password_path = _require_observability_value(manifest.redis_exporter_password_file)
    validated_postgres_password = validate_external_regular_file(
        postgres_password_path,
        repository_root,
        POSTGRES_EXPORTER_PASSWORD_FILE_POLICY,
    )
    validated_redis_password = validate_external_regular_file(
        redis_password_path,
        repository_root,
        REDIS_EXPORTER_PASSWORD_FILE_POLICY,
    )
    _read_exporter_password(
        validated_postgres_password,
        error_code="postgres_exporter_password_content_invalid",
    )

    postgres_uri = _require_observability_value(manifest.postgres_exporter_uri)
    try:
        parsed_postgres = urlsplit(f"postgresql://{postgres_uri}")
        postgres_query = parse_qs(
            parsed_postgres.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        postgres_port = parsed_postgres.port or 5432
    except ValueError as error:
        raise AwsDemoDeploymentValidationError("postgres_exporter_uri_invalid") from error
    app_database = make_url(settings.database_url)
    if (
        parsed_postgres.username is not None
        or parsed_postgres.password is not None
        or is_local_service_host(parsed_postgres.hostname)
        or parsed_postgres.hostname != app_database.host
        or postgres_port != (app_database.port or 5432)
        or parsed_postgres.path.removeprefix("/") != app_database.database
        or parsed_postgres.fragment
        or postgres_query
        != {
            "sslmode": ["verify-full"],
            "sslrootcert": [AWS_DEMO_POSTGRES_CA_CONTAINER_PATH],
        }
    ):
        raise AwsDemoDeploymentValidationError("postgres_exporter_uri_invalid")
    postgres_user = _require_observability_value(manifest.postgres_exporter_user)
    if postgres_user == app_database.username:
        raise AwsDemoDeploymentValidationError("postgres_exporter_user_not_dedicated")

    redis_addr = _require_observability_value(manifest.redis_exporter_addr)
    try:
        parsed_redis = urlsplit(redis_addr)
        redis_port = parsed_redis.port or 6379
    except ValueError as error:
        raise AwsDemoDeploymentValidationError("redis_exporter_addr_invalid") from error
    app_redis = urlsplit(settings.redis_url)
    if (
        parsed_redis.scheme != "rediss"
        or parsed_redis.username is not None
        or parsed_redis.password is not None
        or is_local_service_host(parsed_redis.hostname)
        or parsed_redis.hostname != app_redis.hostname
        or redis_port != (app_redis.port or 6379)
        or parsed_redis.path != app_redis.path
        or parsed_redis.query
        or parsed_redis.fragment
    ):
        raise AwsDemoDeploymentValidationError("redis_exporter_addr_invalid")
    redis_user = _require_observability_value(manifest.redis_exporter_user)
    if redis_user == _decode_url_username(app_redis.username):
        raise AwsDemoDeploymentValidationError("redis_exporter_user_not_dedicated")
    redis_password_key = urlunsplit(
        (
            parsed_redis.scheme,
            f"{redis_user}@{parsed_redis.netloc}",
            parsed_redis.path,
            "",
            "",
        )
    )
    redis_password_body = _read_bounded_external_file(
        validated_redis_password,
        error_code="redis_exporter_password_content_invalid",
    )
    try:
        password_mapping: object = json.loads(redis_password_body)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AwsDemoDeploymentValidationError("redis_exporter_password_content_invalid") from error
    if not isinstance(password_mapping, dict) or set(password_mapping) != {redis_password_key}:
        raise AwsDemoDeploymentValidationError("redis_exporter_password_content_invalid")
    redis_password = password_mapping[redis_password_key]
    if not isinstance(redis_password, str):
        raise AwsDemoDeploymentValidationError("redis_exporter_password_content_invalid")
    encoded_redis_password = redis_password.encode("ascii", errors="ignore")
    if encoded_redis_password.decode("ascii") != redis_password:
        raise AwsDemoDeploymentValidationError("redis_exporter_password_content_invalid")
    if (
        not 16 <= len(encoded_redis_password) <= MAXIMUM_EXPORTER_PASSWORD_BYTES
        or any(character.isspace() or ord(character) < 0x21 for character in redis_password)
        or _contains_known_placeholder(redis_password)
    ):
        raise AwsDemoDeploymentValidationError("redis_exporter_password_content_invalid")
    return postgres_password_path, redis_password_path


def _load_and_validate_load_test_manifest_file(
    release: AwsDemoReleaseManifest,
    settings: Settings,
    repository_root: Path,
    *,
    require_release_artifact_identity: bool,
) -> tuple[Path, LoadTestManifest] | None:
    """共有run manifestをstrict検証し、candidateへの誤帰属を拒否する。"""

    if release.round_scope != "load-test-ten-round":
        return None
    manifest_path = _require_observability_value(release.load_test_manifest_file)
    validated = validate_external_regular_file(
        manifest_path,
        repository_root,
        LOAD_TEST_MANIFEST_FILE_POLICY,
    )
    body = _read_bounded_external_file(
        validated,
        error_code="load_test_manifest_content_invalid",
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        manifest = LoadTestManifest.model_validate_json(body)
    except ValidationError as error:
        raise AwsDemoDeploymentValidationError("load_test_manifest_content_invalid") from error
    if manifest.event.id != release.event_id:
        raise AwsDemoDeploymentValidationError("load_test_manifest_event_identity_mismatch")
    runtime_origins = [
        str(origin).removesuffix("/") for origin in settings.realtime_allowed_origins
    ]
    if runtime_origins != [manifest.public_origin]:
        raise AwsDemoDeploymentValidationError("load_test_manifest_public_origin_mismatch")
    if require_release_artifact_identity and (
        manifest.artifacts.source_commit != release.source_commit
        or manifest.artifacts.api_image != release.api_image
        or manifest.artifacts.gateway_image != release.gateway_image
    ):
        raise AwsDemoDeploymentValidationError("load_test_manifest_release_identity_mismatch")
    return manifest_path, manifest


def validate_load_test_manifest_file(
    release: AwsDemoReleaseManifest,
    settings: Settings,
    repository_root: Path,
    *,
    require_release_artifact_identity: bool,
) -> Path | None:
    """共有run manifestを検証し、互換APIとして検証済みpathを返す。"""

    validated = _load_and_validate_load_test_manifest_file(
        release,
        settings,
        repository_root,
        require_release_artifact_identity=require_release_artifact_identity,
    )
    return validated[0] if validated is not None else None


def _validate_load_test_release_gate(
    release: AwsDemoReleaseManifest,
    settings: Settings,
    repository_root: Path,
    *,
    require_release_artifact_identity: bool,
) -> tuple[Path, ...]:
    validated_manifest = _load_and_validate_load_test_manifest_file(
        release,
        settings,
        repository_root,
        require_release_artifact_identity=require_release_artifact_identity,
    )
    if validated_manifest is None:
        return ()
    return (validated_manifest[0],)


def validate_fault_topology_configuration(
    release: AwsDemoReleaseManifest,
    settings: Settings,
    repository_root: Path,
) -> Path | None:
    """load workloadのRedis経路を外部Toxiproxy設定へ固定する。"""

    if release.round_scope != "load-test-ten-round":
        return None
    config_path = _require_observability_value(release.toxiproxy_config_file)
    validate_external_fault_topology(
        config_path,
        _require_observability_value(release.toxiproxy_config_sha256),
        repository_root,
        settings.redis_url,
        _require_observability_value(release.redis_hostname),
    )
    return config_path


def validate_metrics_artifact_host_directory(
    manifest: AwsDemoReleaseManifest,
    repository_root: Path,
    *,
    protected_paths: tuple[Path, ...],
) -> None:
    """one-shot exporterの唯一のRW bindを専用directoryへ限定する。"""

    if manifest.round_scope != "load-test-ten-round":
        return
    directory = _require_observability_value(manifest.metrics_artifact_host_directory)
    validate_external_artifact_directory(
        directory,
        repository_root,
        runtime_uid=manifest.runtime_uid,
        runtime_gid=manifest.runtime_gid,
        protected_paths=protected_paths,
    )


def validate_runtime_settings(
    manifest: AwsDemoReleaseManifest,
    repository_root: Path,
) -> Settings:
    """明示fileだけからAWS demo runtime設定を構築する。"""

    validate_runtime_env_file_permissions(manifest.runtime_env_file, repository_root)
    raw_values = parse_strict_env_file(
        manifest.runtime_env_file,
        frozenset(RUNTIME_ENVIRONMENT_FIELDS),
    )
    placeholder_fields = tuple(
        sorted(
            field
            for field in SECRET_ENVIRONMENT_FIELDS
            if _contains_known_placeholder(raw_values[field])
        )
    )
    if placeholder_fields:
        raise AwsDemoDeploymentValidationError(
            "runtime_environment_contains_placeholder",
            placeholder_fields,
        )
    settings_values = {
        RUNTIME_ENVIRONMENT_FIELDS[field]: value for field, value in raw_values.items()
    }
    try:
        settings_values["realtime_allowed_origins"] = REALTIME_ORIGINS_ADAPTER.validate_json(
            raw_values["REALTIME_ALLOWED_ORIGINS"]
        )
    except ValidationError as error:
        raise AwsDemoDeploymentValidationError(
            "runtime_environment_invalid",
            ("REALTIME_ALLOWED_ORIGINS",),
        ) from error
    try:
        settings = Settings(_env_file=None, **settings_values)
    except ValidationError as error:
        fields = tuple(sorted(_validation_error_fields(error)))
        raise AwsDemoDeploymentValidationError("runtime_environment_invalid", fields) from error
    if settings.app_env is not AppEnvironment.AWS_DEMO:
        raise AwsDemoDeploymentValidationError("runtime_environment_not_aws_demo")
    if settings.app_version != manifest.source_commit:
        raise AwsDemoDeploymentValidationError("runtime_release_commit_mismatch", ("APP_VERSION",))
    if settings.aws_demo_round_scope != manifest.round_scope:
        raise AwsDemoDeploymentValidationError(
            "runtime_release_round_scope_mismatch",
            ("AWS_DEMO_ROUND_SCOPE",),
        )
    if (
        manifest.round_scope != "single-round"
        and str(settings.round_backup_directory) != AWS_DEMO_ROUND_BACKUP_CONTAINER_PATH
    ):
        raise AwsDemoDeploymentValidationError(
            "runtime_round_backup_container_path_mismatch",
            ("ROUND_BACKUP_DIRECTORY",),
        )
    return settings


def validate_aws_demo_deployment(
    release_file: Path,
    rollback_file: Path | None,
    repository_root: Path,
) -> ValidatedAwsDemoDeployment:
    """candidateと指定済みrollbackを検証し、6問リハーサルのみcandidate単独を許可する。"""

    release = validate_release_manifest_file(release_file, repository_root)
    if rollback_file is None:
        if release.round_scope != "rehearsal-six-round":
            raise AwsDemoDeploymentValidationError("rollback_manifest_required")
        validate_release_execution_identity(release)
        validate_postgres_ca_file(release, repository_root)
        release_settings = validate_runtime_settings(release, repository_root)
        validate_round_backup_host_directory(
            release,
            repository_root,
            protected_paths=(release_file, release.runtime_env_file, release.postgres_ca_file),
        )
        return ValidatedAwsDemoDeployment(
            release=release,
            rollback=None,
            release_settings=release_settings,
            rollback_settings=None,
        )
    rollback = validate_release_manifest_file(rollback_file, repository_root)
    _validate_release_pair(release, rollback)
    validate_release_execution_identity(release)
    validate_release_execution_identity(rollback)
    validate_postgres_ca_file(release, repository_root)
    validate_postgres_ca_file(rollback, repository_root)
    release_settings = validate_runtime_settings(release, repository_root)
    rollback_settings = validate_runtime_settings(rollback, repository_root)
    release_observability_paths = validate_observability_configuration(
        release,
        release_settings,
        repository_root,
    )
    rollback_observability_paths = validate_observability_configuration(
        rollback,
        rollback_settings,
        repository_root,
    )
    release_fault_topology_path = validate_fault_topology_configuration(
        release,
        release_settings,
        repository_root,
    )
    rollback_fault_topology_path = validate_fault_topology_configuration(
        rollback,
        rollback_settings,
        repository_root,
    )
    load_test_control_paths = _validate_load_test_release_gate(
        release,
        release_settings,
        repository_root,
        require_release_artifact_identity=True,
    )
    protected_paths = (
        release_file,
        rollback_file,
        release.runtime_env_file,
        rollback.runtime_env_file,
        release.postgres_ca_file,
        rollback.postgres_ca_file,
        *release_observability_paths,
        *rollback_observability_paths,
        *load_test_control_paths,
        *((release_fault_topology_path,) if release_fault_topology_path is not None else ()),
        *((rollback_fault_topology_path,) if rollback_fault_topology_path is not None else ()),
        *tuple(
            path
            for path in (
                release.metrics_artifact_host_directory,
                rollback.metrics_artifact_host_directory,
            )
            if path is not None
        ),
    )
    validate_round_backup_host_directory(
        release,
        repository_root,
        protected_paths=protected_paths,
    )
    validate_metrics_artifact_host_directory(
        release,
        repository_root,
        protected_paths=tuple(
            path
            for path in protected_paths
            if path
            not in {
                release.metrics_artifact_host_directory,
                rollback.metrics_artifact_host_directory,
            }
        )
        + tuple(
            path
            for path in (
                release.round_backup_host_directory,
                rollback.round_backup_host_directory,
            )
            if path is not None
        ),
    )
    validate_metrics_artifact_host_directory(
        rollback,
        repository_root,
        protected_paths=tuple(
            path
            for path in protected_paths
            if path
            not in {
                release.metrics_artifact_host_directory,
                rollback.metrics_artifact_host_directory,
            }
        )
        + tuple(
            path
            for path in (
                release.round_backup_host_directory,
                rollback.round_backup_host_directory,
            )
            if path is not None
        ),
    )
    validate_round_backup_host_directory(
        rollback,
        repository_root,
        protected_paths=protected_paths,
    )

    identity_mismatches = _runtime_identity_mismatches(release_settings, rollback_settings)
    if identity_mismatches:
        raise AwsDemoDeploymentValidationError(
            "rollback_runtime_identity_mismatch",
            identity_mismatches,
        )

    return ValidatedAwsDemoDeployment(
        release=release,
        rollback=rollback,
        release_settings=release_settings,
        rollback_settings=rollback_settings,
    )


def validate_aws_demo_rollback(
    release_file: Path,
    failed_release_file: Path,
    repository_root: Path,
) -> ValidatedAwsDemoRollback:
    """直前成功releaseだけを実行検証し、壊れたfallback runtimeへ依存せず切り戻す。"""

    release = validate_release_manifest_file(release_file, repository_root)
    failed_release = validate_release_manifest_file(failed_release_file, repository_root)
    _validate_release_pair(release, failed_release)
    validate_release_execution_identity(release)
    validate_postgres_ca_file(release, repository_root)
    release_settings = validate_runtime_settings(release, repository_root)
    release_observability_paths = validate_observability_configuration(
        release,
        release_settings,
        repository_root,
    )
    release_fault_topology_path = validate_fault_topology_configuration(
        release,
        release_settings,
        repository_root,
    )
    load_test_control_paths = _validate_load_test_release_gate(
        release,
        release_settings,
        repository_root,
        require_release_artifact_identity=False,
    )
    validate_round_backup_host_directory(
        release,
        repository_root,
        protected_paths=(
            release_file,
            failed_release_file,
            release.runtime_env_file,
            release.postgres_ca_file,
            *release_observability_paths,
            *load_test_control_paths,
            *((release_fault_topology_path,) if release_fault_topology_path is not None else ()),
        ),
        optional_protected_paths=(
            failed_release.runtime_env_file,
            failed_release.postgres_ca_file,
            *tuple(
                path
                for path in (
                    failed_release.postgres_exporter_password_file,
                    failed_release.redis_exporter_password_file,
                    failed_release.metrics_artifact_host_directory,
                    failed_release.toxiproxy_config_file,
                )
                if path is not None
            ),
        ),
    )
    validate_metrics_artifact_host_directory(
        release,
        repository_root,
        protected_paths=(
            release_file,
            failed_release_file,
            release.runtime_env_file,
            release.postgres_ca_file,
            *release_observability_paths,
            *load_test_control_paths,
            *((release_fault_topology_path,) if release_fault_topology_path is not None else ()),
            *(
                (release.round_backup_host_directory,)
                if release.round_backup_host_directory is not None
                else ()
            ),
        ),
    )
    return ValidatedAwsDemoRollback(
        release=release,
        failed_release=failed_release,
        release_settings=release_settings,
    )


def _validate_release_pair(
    release: AwsDemoReleaseManifest,
    rollback: AwsDemoReleaseManifest,
) -> None:
    """candidate/fallback manifestのapplication rollback互換条件を検証する。"""

    if release.release_id == rollback.release_id:
        raise AwsDemoDeploymentValidationError("rollback_release_must_be_distinct")
    if (
        release.round_scope != "load-test-ten-round"
        and release.api_image == rollback.api_image
        and release.gateway_image == rollback.gateway_image
    ):
        raise AwsDemoDeploymentValidationError("rollback_images_must_be_distinct")
    if release.schema_revision != rollback.schema_revision:
        raise AwsDemoDeploymentValidationError("rollback_schema_revision_mismatch")
    if release.postgres_ca_sha256 != rollback.postgres_ca_sha256:
        raise AwsDemoDeploymentValidationError("rollback_postgres_ca_mismatch")
    if release.execution_host != rollback.execution_host:
        raise AwsDemoDeploymentValidationError("rollback_execution_host_mismatch")
    if (
        release.docker_context != rollback.docker_context
        or release.docker_endpoint != rollback.docker_endpoint
    ):
        raise AwsDemoDeploymentValidationError("rollback_docker_target_mismatch")
    if (
        release.runtime_uid != rollback.runtime_uid
        or release.runtime_gid != rollback.runtime_gid
        or release.round_scope != rollback.round_scope
        or release.round_backup_host_directory != rollback.round_backup_host_directory
    ):
        raise AwsDemoDeploymentValidationError("rollback_topology_mismatch")
    observability_identity = (
        "prometheus_image",
        "postgres_exporter_image",
        "redis_exporter_image",
        "node_exporter_image",
        "prometheus_config_sha256",
        "postgres_exporter_uri",
        "postgres_exporter_user",
        "postgres_exporter_password_file",
        "redis_exporter_addr",
        "redis_exporter_user",
        "redis_exporter_password_file",
        "metrics_artifact_host_directory",
        "load_test_manifest_file",
    )
    if any(getattr(release, field) != getattr(rollback, field) for field in observability_identity):
        raise AwsDemoDeploymentValidationError("rollback_observability_identity_mismatch")
    fault_topology_identity = (
        "toxiproxy_image",
        "toxiproxy_config_file",
        "toxiproxy_config_sha256",
        "redis_hostname",
    )
    if any(
        getattr(release, field) != getattr(rollback, field) for field in fault_topology_identity
    ):
        raise AwsDemoDeploymentValidationError("rollback_fault_topology_identity_mismatch")
    if release.event_id != rollback.event_id:
        raise AwsDemoDeploymentValidationError("rollback_event_identity_mismatch")
    if release.gateway_port != rollback.gateway_port:
        raise AwsDemoDeploymentValidationError("rollback_gateway_port_mismatch")


def _contains_known_placeholder(value: str) -> bool:
    normalized = value.casefold()
    return not value or any(fragment in normalized for fragment in KNOWN_PLACEHOLDER_FRAGMENTS)


def _runtime_identity_mismatches(release: Settings, rollback: Settings) -> tuple[str, ...]:
    """rollback時に接続先・認証境界・Originを別環境へ切り替えない。"""

    mismatches: list[str] = []
    comparable_values = (
        ("DATABASE_URL", release.database_url, rollback.database_url),
        ("REDIS_URL", release.redis_url, rollback.redis_url),
        (
            "DEMO_OPERATOR_USERNAME",
            release.demo_operator_username,
            rollback.demo_operator_username,
        ),
        (
            "DEMO_OPERATOR_PASSWORD",
            release.demo_operator_password.get_secret_value(),
            rollback.demo_operator_password.get_secret_value(),
        ),
        (
            "SESSION_SECRET",
            release.session_secret.get_secret_value(),
            rollback.session_secret.get_secret_value(),
        ),
        (
            "PGSSLROOTCERT",
            str(release.postgres_ssl_root_cert),
            str(rollback.postgres_ssl_root_cert),
        ),
        (
            "AWS_DEMO_ROUND_SCOPE",
            release.aws_demo_round_scope or "",
            rollback.aws_demo_round_scope or "",
        ),
        (
            "ROUND_BACKUP_STORAGE_MODE",
            release.round_backup_storage_mode,
            rollback.round_backup_storage_mode,
        ),
        (
            "ROUND_BACKUP_DIRECTORY",
            str(release.round_backup_directory),
            str(rollback.round_backup_directory),
        ),
        (
            "ROUND_BACKUP_ENCRYPTION_KEY",
            release.round_backup_encryption_key.get_secret_value(),
            rollback.round_backup_encryption_key.get_secret_value(),
        ),
    )
    for field, release_value, rollback_value in comparable_values:
        if not hmac.compare_digest(release_value.encode(), rollback_value.encode()):
            mismatches.append(field)
    release_origins = tuple(str(origin) for origin in release.realtime_allowed_origins)
    rollback_origins = tuple(str(origin) for origin in rollback.realtime_allowed_origins)
    if release_origins != rollback_origins:
        mismatches.append("REALTIME_ALLOWED_ORIGINS")
    return tuple(sorted(mismatches))


def _validation_error_fields(error: ValidationError) -> set[str]:
    fields: set[str] = set()
    for detail in error.errors(include_input=False, include_url=False):
        location = detail.get("loc", ())
        if location:
            fields.add(".".join(str(part) for part in location))
        else:
            fields.add("configuration")
    return fields


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AWS demo配置設定を安全条件だけで検証する")
    parser.add_argument("--release-file", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--rollback-file", type=Path)
    parser.add_argument("--rollback-operation", action="store_true")
    parser.add_argument("--field", choices=tuple(SAFE_OUTPUT_FIELDS))
    return parser


def main(argv: list[str] | None = None) -> int:
    """検証結果または非秘密release fieldだけを標準出力へ返す。"""

    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.field is not None:
            manifest = validate_release_manifest_file(
                arguments.release_file,
                arguments.repository_root,
            )
            field_name = SAFE_OUTPUT_FIELDS[arguments.field]
            print(getattr(manifest, field_name))
            return os.EX_OK
        if arguments.rollback_operation:
            if arguments.rollback_file is None:
                raise AwsDemoDeploymentValidationError("rollback_manifest_required")
            rollback = validate_aws_demo_rollback(
                arguments.release_file,
                arguments.rollback_file,
                arguments.repository_root,
            )
            success_message = (
                "AWS demo rollback configuration passed "
                f"(release: {rollback.release.release_id}, "
                f"failed: {rollback.failed_release.release_id})."
            )
        else:
            deployment = validate_aws_demo_deployment(
                arguments.release_file,
                arguments.rollback_file,
                arguments.repository_root,
            )
            success_message = (
                "AWS demo deployment configuration passed "
                f"(release: {deployment.release.release_id}, "
                f"rollback: {deployment.rollback.release_id if deployment.rollback else 'none'})."
            )
    except AwsDemoDeploymentValidationError as error:
        field_suffix = f" (fields: {','.join(error.fields)})" if error.fields else ""
        print(f"Error: {error.code}{field_suffix}", file=sys.stderr)
        return os.EX_CONFIG

    print(success_message)
    return os.EX_OK


if __name__ == "__main__":
    raise SystemExit(main())
