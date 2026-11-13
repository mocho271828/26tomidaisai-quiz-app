"""障害注入hostが使用するexternal release identityの検証。"""

from __future__ import annotations

import os
import pwd
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import ValidationError

from quiz_app.aws_demo_paths import (
    LOAD_TEST_MANIFEST_FILE_POLICY,
    RELEASE_MANIFEST_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.config import is_local_service_host
from quiz_app.load_test.controller_contracts import canonical_json_bytes
from quiz_app.load_test.fault_controller_contracts import FaultReleaseIdentity
from quiz_app.load_test.fault_topology import (
    ToxiproxyRedisProxy,
    validate_external_toxiproxy_proxy,
)
from quiz_app.load_test.manifest import MAX_MANIFEST_BYTES, LoadTestManifest
from quiz_app.schema_revision import SCHEMA_REVISION

DOCKER_ENDPOINT: Final = "unix:///var/run/docker.sock"
MAXIMUM_RELEASE_BYTES: Final = 64 * 1024

_HEX_40: Final = re.compile(r"^[0-9a-f]{40}$")
_HEX_64: Final = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_DOCKER_CONTEXT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_EXECUTION_HOST: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{1,251}[a-z0-9]$")
_SAFE_PATH_ARGUMENT: Final = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
_IMAGE_REFERENCE: Final = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_EXPORTER_USERNAME: Final = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
_DNS_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_LOAD_RELEASE_FIELDS: Final[frozenset[str]] = frozenset(
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
        "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY",
        "AWS_DEMO_PROMETHEUS_IMAGE",
        "AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
        "AWS_DEMO_REDIS_EXPORTER_IMAGE",
        "AWS_DEMO_NODE_EXPORTER_IMAGE",
        "AWS_DEMO_TOXIPROXY_IMAGE",
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256",
        "AWS_DEMO_REDIS_HOSTNAME",
        "AWS_DEMO_PROMETHEUS_CONFIG_SHA256",
        "AWS_DEMO_POSTGRES_EXPORTER_URI",
        "AWS_DEMO_POSTGRES_EXPORTER_USER",
        "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
        "AWS_DEMO_REDIS_EXPORTER_ADDR",
        "AWS_DEMO_REDIS_EXPORTER_USER",
        "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
        "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
        "AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
        "AWS_DEMO_SCHEMA_REVISION",
        "AWS_DEMO_GATEWAY_PORT",
    }
)

_PAIR_IDENTITY_FIELDS: Final = (
    "AWS_DEMO_POSTGRES_CA_SHA256",
    "AWS_DEMO_EXECUTION_HOST",
    "AWS_DEMO_DOCKER_CONTEXT",
    "AWS_DEMO_DOCKER_ENDPOINT",
    "AWS_DEMO_RUNTIME_UID",
    "AWS_DEMO_RUNTIME_GID",
    "AWS_DEMO_EVENT_ID",
    "AWS_DEMO_ROUND_SCOPE",
    "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY",
    "AWS_DEMO_PROMETHEUS_IMAGE",
    "AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
    "AWS_DEMO_REDIS_EXPORTER_IMAGE",
    "AWS_DEMO_NODE_EXPORTER_IMAGE",
    "AWS_DEMO_TOXIPROXY_IMAGE",
    "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
    "AWS_DEMO_TOXIPROXY_CONFIG_SHA256",
    "AWS_DEMO_REDIS_HOSTNAME",
    "AWS_DEMO_PROMETHEUS_CONFIG_SHA256",
    "AWS_DEMO_POSTGRES_EXPORTER_URI",
    "AWS_DEMO_POSTGRES_EXPORTER_USER",
    "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_REDIS_EXPORTER_ADDR",
    "AWS_DEMO_REDIS_EXPORTER_USER",
    "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
    "AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
    "AWS_DEMO_SCHEMA_REVISION",
    "AWS_DEMO_GATEWAY_PORT",
)
_IMAGE_FIELDS: Final = (
    "AWS_DEMO_API_IMAGE",
    "AWS_DEMO_GATEWAY_IMAGE",
    "AWS_DEMO_PROMETHEUS_IMAGE",
    "AWS_DEMO_POSTGRES_EXPORTER_IMAGE",
    "AWS_DEMO_REDIS_EXPORTER_IMAGE",
    "AWS_DEMO_NODE_EXPORTER_IMAGE",
    "AWS_DEMO_TOXIPROXY_IMAGE",
)
_PATH_FIELDS: Final = (
    "AWS_DEMO_RUNTIME_ENV_FILE",
    "AWS_DEMO_POSTGRES_CA_FILE",
    "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY",
    "AWS_DEMO_TOXIPROXY_CONFIG_FILE",
    "AWS_DEMO_POSTGRES_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_REDIS_EXPORTER_PASSWORD_FILE",
    "AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY",
    "AWS_DEMO_LOAD_TEST_MANIFEST_FILE",
)


class FaultControllerHostError(RuntimeError):
    """外部出力やsecretを含めない固定codeのhost adapter error。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FaultMutationPreconditionError(FaultControllerHostError):
    """mutation request送信前に失敗し、状態変更がないと証明できる。"""


class FaultMutationOutcomeUncertainError(FaultControllerHostError):
    """mutation request送信後の失敗で、遅延完了の可能性が残る。"""


class FaultTargetRecoveryPendingError(FaultControllerHostError):
    """recovery中だけ再試行できる、identity検証済みtargetの過渡状態。"""

    def __init__(self, code: str, *, target: str, identity_sha256: str) -> None:
        self.target = target
        self.identity_sha256 = identity_sha256
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FaultHostConfiguration:
    repository_root: Path
    release_file: Path
    rollback_file: Path
    manifest_file: Path
    manifest: LoadTestManifest
    manifest_sha256: str
    release_identity: FaultReleaseIdentity
    release_environment: tuple[tuple[str, str], ...]
    rollback_environment: tuple[tuple[str, str], ...]
    execution_host: str
    docker_context: str
    docker_endpoint: Literal["unix:///var/run/docker.sock"]
    api_image: str
    toxiproxy_image: str
    toxiproxy_configuration_file: Path
    redis_hostname: str
    toxiproxy_proxy: ToxiproxyRedisProxy
    toxiproxy_configuration_sha256: str
    home_directory: Path


def _parse_env_body(body: bytes) -> dict[str, str]:
    try:
        lines = body.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise FaultControllerHostError("fault_host_release_invalid") from error
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or key in values
            or not value
            or value != value.strip()
            or not value.isprintable()
            or any(character.isspace() for character in value)
            or any(character in value for character in ("$", "%", "\\", "#", "\x00", "'", '"'))
        ):
            raise FaultControllerHostError("fault_host_release_invalid")
        values[key] = value
    if frozenset(values) != _LOAD_RELEASE_FIELDS:
        raise FaultControllerHostError("fault_host_release_fields_invalid")
    return values


def _read_external(
    path: Path,
    repository_root: Path,
    *,
    manifest: bool,
) -> tuple[Path, bytes]:
    try:
        validated = validate_external_regular_file(
            path,
            repository_root,
            LOAD_TEST_MANIFEST_FILE_POLICY if manifest else RELEASE_MANIFEST_FILE_POLICY,
        )
        body = read_validated_external_file(
            validated,
            maximum_bytes=MAX_MANIFEST_BYTES if manifest else MAXIMUM_RELEASE_BYTES,
            unavailable_code=(
                "fault_host_manifest_unavailable" if manifest else "fault_host_release_unavailable"
            ),
        )
    except AwsDemoDeploymentValidationError as error:
        code = "fault_host_manifest_invalid" if manifest else "fault_host_release_invalid"
        raise FaultControllerHostError(code) from error
    return validated.resolved, body


def _require_pattern(value: str, pattern: re.Pattern[str], code: str) -> str:
    if pattern.fullmatch(value) is None:
        raise FaultControllerHostError(code)
    return value


def _canonical_positive_id(value: str, code: str) -> int:
    if not value.isdecimal() or value.startswith("0"):
        raise FaultControllerHostError(code)
    parsed = int(value)
    if not 1 <= parsed <= 2_147_483_647 or str(parsed) != value:
        raise FaultControllerHostError(code)
    return parsed


def _validate_release_values(values: Mapping[str, str]) -> None:
    _require_pattern(values["AWS_DEMO_RELEASE_ID"], _RELEASE_ID, "fault_host_release_invalid")
    _require_pattern(values["AWS_DEMO_SOURCE_COMMIT"], _HEX_40, "fault_host_release_invalid")
    _require_pattern(
        values["AWS_DEMO_TOXIPROXY_CONFIG_SHA256"], _HEX_64, "fault_host_release_invalid"
    )
    for field in (
        "AWS_DEMO_POSTGRES_CA_SHA256",
        "AWS_DEMO_PROMETHEUS_CONFIG_SHA256",
    ):
        _require_pattern(values[field], _HEX_64, "fault_host_release_invalid")
    _require_pattern(
        values["AWS_DEMO_DOCKER_CONTEXT"], _DOCKER_CONTEXT, "fault_host_release_invalid"
    )
    if values["AWS_DEMO_DOCKER_CONTEXT"] != "default":
        raise FaultControllerHostError("fault_host_docker_endpoint_invalid")
    host = _require_pattern(
        values["AWS_DEMO_EXECUTION_HOST"], _EXECUTION_HOST, "fault_host_release_invalid"
    )
    if is_local_service_host(host) or not _is_dns_name(host):
        raise FaultControllerHostError("fault_host_release_invalid")
    if values["AWS_DEMO_DOCKER_ENDPOINT"] != DOCKER_ENDPOINT:
        raise FaultControllerHostError("fault_host_docker_endpoint_invalid")
    if values["AWS_DEMO_ROUND_SCOPE"] != "load-test-ten-round":
        raise FaultControllerHostError("fault_host_round_scope_invalid")
    if values["AWS_DEMO_SCHEMA_REVISION"] != SCHEMA_REVISION:
        raise FaultControllerHostError("fault_host_schema_revision_invalid")
    gateway_port = _canonical_positive_id(
        values["AWS_DEMO_GATEWAY_PORT"], "fault_host_gateway_port_invalid"
    )
    if not 1_024 <= gateway_port <= 65_535:
        raise FaultControllerHostError("fault_host_gateway_port_invalid")
    for field in ("AWS_DEMO_POSTGRES_EXPORTER_USER", "AWS_DEMO_REDIS_EXPORTER_USER"):
        _require_pattern(values[field], _EXPORTER_USERNAME, "fault_host_release_invalid")
    _canonical_positive_id(values["AWS_DEMO_RUNTIME_UID"], "fault_host_runtime_identity_invalid")
    _canonical_positive_id(values["AWS_DEMO_RUNTIME_GID"], "fault_host_runtime_identity_invalid")
    try:
        event_id = UUID(values["AWS_DEMO_EVENT_ID"])
    except ValueError as error:
        raise FaultControllerHostError("fault_host_event_identity_invalid") from error
    if str(event_id) != values["AWS_DEMO_EVENT_ID"]:
        raise FaultControllerHostError("fault_host_event_identity_invalid")
    if any(_IMAGE_REFERENCE.fullmatch(values[field]) is None for field in _IMAGE_FIELDS):
        raise FaultControllerHostError("fault_host_release_invalid")
    for field in _PATH_FIELDS:
        path = Path(values[field])
        if (
            not path.is_absolute()
            or path == Path("/")
            or ".." in path.parts
            or _SAFE_PATH_ARGUMENT.fullmatch(values[field]) is None
        ):
            raise FaultControllerHostError("fault_host_release_invalid")
    _validate_secret_free_exporter_endpoints(values)


def _is_dns_name(value: str) -> bool:
    return len(value) <= 253 and all(_DNS_LABEL.fullmatch(label) for label in value.split("."))


def _validate_secret_free_exporter_endpoints(values: Mapping[str, str]) -> None:
    try:
        postgres = urlsplit(f"postgresql://{values['AWS_DEMO_POSTGRES_EXPORTER_URI']}")
        postgres_port = postgres.port or 5432
        postgres_query = parse_qs(postgres.query, keep_blank_values=True, strict_parsing=True)
        redis = urlsplit(values["AWS_DEMO_REDIS_EXPORTER_ADDR"])
        redis_port = redis.port or 6379
    except ValueError as error:
        raise FaultControllerHostError("fault_host_release_invalid") from error
    if (
        postgres.username is not None
        or postgres.password is not None
        or is_local_service_host(postgres.hostname)
        or postgres.hostname is None
        or not _is_dns_name(postgres.hostname)
        or not 1 <= postgres_port <= 65535
        or not postgres.path.removeprefix("/")
        or postgres.fragment
        or postgres_query
        != {
            "sslmode": ["verify-full"],
            "sslrootcert": ["/run/secrets/aws-demo-postgres-ca.pem"],
        }
        or redis.scheme != "rediss"
        or redis.username is not None
        or redis.password is not None
        or redis.hostname != values["AWS_DEMO_REDIS_HOSTNAME"]
        or is_local_service_host(redis.hostname)
        or redis.hostname is None
        or not _is_dns_name(redis.hostname)
        or not 1 <= redis_port <= 65535
        or redis.query
        or redis.fragment
    ):
        raise FaultControllerHostError("fault_host_release_invalid")


def _safe_home_directory() -> Path:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        status = home.lstat()
        resolved = home.resolve(strict=True)
    except (KeyError, OSError) as error:
        raise FaultControllerHostError("fault_host_home_invalid") from error
    if (
        not home.is_absolute()
        or home.is_symlink()
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o022
        or _SAFE_PATH_ARGUMENT.fullmatch(str(resolved)) is None
    ):
        raise FaultControllerHostError("fault_host_home_invalid")
    return resolved


def load_fault_host_configuration(
    release_file: Path,
    rollback_file: Path,
    repository_root: Path,
) -> FaultHostConfiguration:
    """external release/manifestを安全にreadし、candidate identityへ固定する。"""

    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        raise FaultControllerHostError("fault_host_repository_invalid") from error
    if (
        not all(
            (root / relative).is_file()
            for relative in (
                "backend/pyproject.toml",
                "deploy/aws-demo/compose.yaml",
                "deploy/aws-demo/compose.load-test.yaml",
            )
        )
        or _SAFE_PATH_ARGUMENT.fullmatch(str(root)) is None
    ):
        raise FaultControllerHostError("fault_host_repository_invalid")

    release_path, release_body = _read_external(release_file, root, manifest=False)
    rollback_path, rollback_body = _read_external(rollback_file, root, manifest=False)
    release = _parse_env_body(release_body)
    rollback = _parse_env_body(rollback_body)
    _validate_release_values(release)
    _validate_release_values(rollback)
    if (
        any(release[field] != rollback[field] for field in _PAIR_IDENTITY_FIELDS)
        or release["AWS_DEMO_RELEASE_ID"] == rollback["AWS_DEMO_RELEASE_ID"]
    ):
        raise FaultControllerHostError("fault_host_release_pair_mismatch")
    if (
        int(release["AWS_DEMO_RUNTIME_UID"]) != os.geteuid()
        or int(release["AWS_DEMO_RUNTIME_GID"]) != os.getegid()
    ):
        raise FaultControllerHostError("fault_host_runtime_identity_invalid")

    manifest_input = Path(release["AWS_DEMO_LOAD_TEST_MANIFEST_FILE"])
    manifest_path, manifest_body = _read_external(manifest_input, root, manifest=True)
    try:
        manifest = LoadTestManifest.model_validate_json(manifest_body, strict=True)
    except ValidationError as error:
        raise FaultControllerHostError("fault_host_manifest_invalid") from error
    if (
        str(manifest.event.id) != release["AWS_DEMO_EVENT_ID"]
        or manifest.artifacts.source_commit != release["AWS_DEMO_SOURCE_COMMIT"]
        or manifest.artifacts.api_image != release["AWS_DEMO_API_IMAGE"]
        or manifest.artifacts.gateway_image != release["AWS_DEMO_GATEWAY_IMAGE"]
    ):
        raise FaultControllerHostError("fault_host_manifest_identity_mismatch")
    try:
        toxiproxy_proxy = validate_external_toxiproxy_proxy(
            Path(release["AWS_DEMO_TOXIPROXY_CONFIG_FILE"]),
            release["AWS_DEMO_TOXIPROXY_CONFIG_SHA256"],
            root,
        )
    except AwsDemoDeploymentValidationError as error:
        raise FaultControllerHostError("fault_host_topology_invalid") from error

    return FaultHostConfiguration(
        repository_root=root,
        release_file=release_path,
        rollback_file=rollback_path,
        manifest_file=manifest_path,
        manifest=manifest,
        manifest_sha256=sha256(canonical_json_bytes(manifest)).hexdigest(),
        release_identity=FaultReleaseIdentity(
            release_id=release["AWS_DEMO_RELEASE_ID"],
            release_manifest_sha256=sha256(release_body).hexdigest(),
            source_commit=release["AWS_DEMO_SOURCE_COMMIT"],
            rollback_release_id=rollback["AWS_DEMO_RELEASE_ID"],
            rollback_manifest_sha256=sha256(rollback_body).hexdigest(),
            execution_host=release["AWS_DEMO_EXECUTION_HOST"],
            docker_context=release["AWS_DEMO_DOCKER_CONTEXT"],
        ),
        release_environment=tuple(sorted(release.items())),
        rollback_environment=tuple(sorted(rollback.items())),
        execution_host=release["AWS_DEMO_EXECUTION_HOST"],
        docker_context=release["AWS_DEMO_DOCKER_CONTEXT"],
        docker_endpoint=DOCKER_ENDPOINT,
        api_image=release["AWS_DEMO_API_IMAGE"],
        toxiproxy_image=release["AWS_DEMO_TOXIPROXY_IMAGE"],
        toxiproxy_configuration_file=Path(release["AWS_DEMO_TOXIPROXY_CONFIG_FILE"]),
        redis_hostname=release["AWS_DEMO_REDIS_HOSTNAME"],
        toxiproxy_proxy=toxiproxy_proxy,
        toxiproxy_configuration_sha256=release["AWS_DEMO_TOXIPROXY_CONFIG_SHA256"],
        home_directory=_safe_home_directory(),
    )
