"""AWS demo負荷試験用Redis障害注入経路のfail-closed契約。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, ip_address
from pathlib import Path
from typing import Annotated, Final, Literal, NoReturn
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictStr,
    StringConstraints,
    ValidationError,
)

from quiz_app.aws_demo_paths import (
    TOXIPROXY_CONFIG_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    ValidatedExternalPath,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.config import is_allowed_aws_remote_address, is_local_service_host
from quiz_app.dependency_hosts import ValidatedDependencyHost

TOXIPROXY_EXPECTED_VERSION: Final = "2.12.0"
TOXIPROXY_EXPECTED_REVISION: Final = "3ccd6a79cbc6c6a72b884d295ad314b75cdf3962"
TOXIPROXY_PROXY_NAME: Final = "gs-app-tomidaisai-aws-demo-redis"
TOXIPROXY_DATA_NETWORK: Final = IPv4Network("172.30.250.0/29")
TOXIPROXY_DATA_ADDRESS: Final = IPv4Address("172.30.250.2")
TOXIPROXY_CONTROL_NETWORK: Final = IPv4Network("172.30.251.0/29")
TOXIPROXY_CONTROL_ADDRESS: Final = IPv4Address("172.30.251.2")
TOXIPROXY_CONTROL_PORT: Final = 8474
TOXIPROXY_CONFIG_CONTAINER_PATH: Final = Path("/run/load-test/toxiproxy.json")
TOXIPROXY_CONFIG_MAX_BYTES: Final = 4096
TOXIPROXY_CONFIG_SHA256_ENV: Final = "AWS_DEMO_TOXIPROXY_CONFIG_SHA256"
TOXIPROXY_EXPECTED_NAME_ENV: Final = "AWS_DEMO_TOXIPROXY_EXPECTED_NAME"
TOXIPROXY_EXPECTED_LISTEN_ENV: Final = "AWS_DEMO_TOXIPROXY_EXPECTED_LISTEN"
TOXIPROXY_EXPECTED_UPSTREAM_ENV: Final = "AWS_DEMO_TOXIPROXY_EXPECTED_UPSTREAM"
REDIS_DEFAULT_PORT: Final = 6379

SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
DNS_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
Endpoint = Annotated[StrictStr, StringConstraints(min_length=3, max_length=255)]


class ToxiproxyRedisProxy(BaseModel):
    """起動時に許可する唯一のRedis TCP proxy定義。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Literal["gs-app-tomidaisai-aws-demo-redis"]
    listen: Endpoint
    upstream: Endpoint
    enabled: Literal[True]


class _ToxiproxyConfiguration(RootModel[tuple[ToxiproxyRedisProxy, ...]]):
    """Toxiproxyのtop-level JSON arrayを1 proxyへ固定する。"""

    model_config = ConfigDict(frozen=True, strict=True)

    root: tuple[ToxiproxyRedisProxy, ...] = Field(min_length=1, max_length=1)


class RuntimeToxiproxyProxySnapshot(BaseModel):
    """短命control clientへ渡す秘密なしのin-memory topology。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    configuration_sha256: Annotated[
        StrictStr,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    proxy: ToxiproxyRedisProxy


class _DuplicateConfigurationKey(ValueError):
    """曖昧なJSON objectを入力値なしで拒否する内部signal。"""


@dataclass(frozen=True, slots=True)
class ValidatedFaultTopology:
    """外部設定・runtime URL・upstreamを相互照合した不変な経路。"""

    config_path: Path | None
    redis_hostname: str
    redis_port: int
    upstream_address: str
    proxy: ToxiproxyRedisProxy


def _raise(code: str, *fields: str) -> NoReturn:
    raise AwsDemoDeploymentValidationError(code, fields)


def _is_remote_fqdn(hostname: str) -> bool:
    if (
        not 1 <= len(hostname) <= 253
        or hostname != hostname.casefold()
        or hostname.endswith(".")
        or is_local_service_host(hostname)
    ):
        return False
    try:
        ip_address(hostname)
    except ValueError:
        return all(DNS_LABEL_PATTERN.fullmatch(label) for label in hostname.split("."))
    return False


def _redis_endpoint(
    redis_url: str,
    expected_redis_hostname: str | None,
) -> tuple[str, int]:
    try:
        parsed = urlsplit(redis_url)
        hostname = unquote(parsed.hostname or "", errors="strict")
        port = parsed.port or REDIS_DEFAULT_PORT
    except (UnicodeError, ValueError):
        _raise("fault_topology_redis_url_invalid", "REDIS_URL")

    expected_hostname = hostname if expected_redis_hostname is None else expected_redis_hostname
    if not _is_remote_fqdn(expected_hostname):
        _raise("fault_topology_redis_hostname_invalid", "AWS_DEMO_REDIS_HOSTNAME")
    if parsed.scheme != "rediss" or not 1 <= port <= 65535:
        _raise("fault_topology_redis_url_invalid", "REDIS_URL")
    if hostname != expected_hostname:
        _raise(
            "toxiproxy_redis_hostname_mismatch",
            "REDIS_URL",
            "AWS_DEMO_REDIS_HOSTNAME",
        )
    return hostname, port


def _parse_ipv4_endpoint(value: str, *, error_code: str) -> tuple[IPv4Address, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text.isascii() or not port_text.isdecimal():
        _raise(error_code)
    try:
        address = ip_address(host)
        port = int(port_text)
    except ValueError:
        _raise(error_code)
    if not isinstance(address, IPv4Address) or not 1 <= port <= 65535 or port_text != str(port):
        _raise(error_code)
    return address, port


def _parse_configuration(body: bytes) -> ToxiproxyRedisProxy:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise _DuplicateConfigurationKey
        return dict(pairs)

    try:
        json.loads(body, object_pairs_hook=reject_duplicate_keys)
        configuration = _ToxiproxyConfiguration.model_validate_json(body, strict=True)
    except (
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateConfigurationKey,
        ValidationError,
    ) as error:
        raise AwsDemoDeploymentValidationError("toxiproxy_config_invalid") from error
    return configuration.root[0]


def _validate_digest(body: bytes, expected_sha256: str) -> None:
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        _raise("toxiproxy_config_sha256_invalid", "AWS_DEMO_TOXIPROXY_CONFIG_SHA256")
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        _raise("toxiproxy_config_checksum_mismatch", "AWS_DEMO_TOXIPROXY_CONFIG_SHA256")


def _validate_proxy_network_contract(
    proxy: ToxiproxyRedisProxy,
    *,
    expected_redis_port: int | None,
) -> tuple[IPv4Address, int]:
    listen_address, listen_port = _parse_ipv4_endpoint(
        proxy.listen,
        error_code="toxiproxy_config_listen_invalid",
    )
    upstream_address, upstream_port = _parse_ipv4_endpoint(
        proxy.upstream,
        error_code="toxiproxy_config_upstream_invalid",
    )
    if listen_address != TOXIPROXY_DATA_ADDRESS:
        _raise("toxiproxy_config_listen_invalid")
    if listen_port != upstream_port or (
        expected_redis_port is not None and listen_port != expected_redis_port
    ):
        fields = ("REDIS_URL",) if expected_redis_port is not None else ()
        _raise("toxiproxy_config_port_mismatch", *fields)
    if (
        not is_allowed_aws_remote_address(upstream_address)
        or upstream_address in TOXIPROXY_DATA_NETWORK
        or upstream_address in TOXIPROXY_CONTROL_NETWORK
    ):
        _raise("toxiproxy_config_upstream_invalid")
    return upstream_address, upstream_port


def _build_topology(
    body: bytes,
    *,
    config_path: Path | None,
    redis_url: str,
    expected_redis_hostname: str | None,
    expected_sha256: str,
) -> ValidatedFaultTopology:
    _validate_digest(body, expected_sha256)
    redis_hostname, redis_port = _redis_endpoint(redis_url, expected_redis_hostname)
    proxy = _parse_configuration(body)
    upstream_address, _ = _validate_proxy_network_contract(
        proxy,
        expected_redis_port=redis_port,
    )
    return ValidatedFaultTopology(
        config_path=config_path,
        redis_hostname=redis_hostname,
        redis_port=redis_port,
        upstream_address=str(upstream_address),
        proxy=proxy,
    )


def validate_external_fault_topology(
    config_path: Path,
    expected_sha256: str,
    repository_root: Path,
    redis_url: str,
    expected_redis_hostname: str,
) -> ValidatedFaultTopology:
    """外部0600設定とRedis URLを検証し、障害注入経路を確定する。"""

    validated = validate_external_regular_file(
        config_path,
        repository_root,
        TOXIPROXY_CONFIG_FILE_POLICY,
    )
    body = read_validated_external_file(
        validated,
        maximum_bytes=TOXIPROXY_CONFIG_MAX_BYTES,
        unavailable_code=TOXIPROXY_CONFIG_FILE_POLICY.unavailable,
    )
    return _build_topology(
        body,
        config_path=validated.resolved,
        redis_url=redis_url,
        expected_redis_hostname=expected_redis_hostname,
        expected_sha256=expected_sha256,
    )


def validate_external_toxiproxy_proxy(
    config_path: Path,
    expected_sha256: str,
    repository_root: Path,
) -> ToxiproxyRedisProxy:
    """外部0600設定からcredential不要の固定proxy identityだけを検証する。"""

    validated = validate_external_regular_file(
        config_path,
        repository_root,
        TOXIPROXY_CONFIG_FILE_POLICY,
    )
    body = read_validated_external_file(
        validated,
        maximum_bytes=TOXIPROXY_CONFIG_MAX_BYTES,
        unavailable_code=TOXIPROXY_CONFIG_FILE_POLICY.unavailable,
    )
    _validate_digest(body, expected_sha256)
    proxy = _parse_configuration(body)
    _validate_proxy_network_contract(proxy, expected_redis_port=None)
    return proxy


def _load_runtime_configuration(config_path: Path) -> tuple[Path, bytes]:
    if not config_path.is_absolute() or ".." in config_path.parts:
        _raise("toxiproxy_config_runtime_path_invalid")
    try:
        path_status = config_path.lstat()
        resolved_path = config_path.resolve(strict=True)
    except OSError as error:
        raise AwsDemoDeploymentValidationError("toxiproxy_config_unavailable") from error
    if (
        stat.S_ISLNK(path_status.st_mode)
        or not stat.S_ISREG(path_status.st_mode)
        or stat.S_IMODE(path_status.st_mode) != 0o600
        or path_status.st_nlink != 1
        or path_status.st_uid != os.geteuid()
    ):
        _raise("toxiproxy_config_unavailable")
    validated = ValidatedExternalPath(
        resolved=resolved_path,
        status=path_status,
        ancestors=(),
    )
    body = read_validated_external_file(
        validated,
        maximum_bytes=TOXIPROXY_CONFIG_MAX_BYTES,
        unavailable_code="toxiproxy_config_unavailable",
    )
    return resolved_path, body


def load_runtime_fault_topology(
    redis_url: str,
    expected_sha256: str,
    config_path: Path = TOXIPROXY_CONFIG_CONTAINER_PATH,
) -> ValidatedFaultTopology:
    """container内の固定mountから設定を安全に読み、起動時契約を再検証する。"""

    resolved_path, body = _load_runtime_configuration(config_path)
    return _build_topology(
        body,
        config_path=resolved_path,
        redis_url=redis_url,
        expected_redis_hostname=None,
        expected_sha256=expected_sha256,
    )


def load_runtime_toxiproxy_proxy(
    expected_sha256: str,
    config_path: Path = TOXIPROXY_CONFIG_CONTAINER_PATH,
) -> ToxiproxyRedisProxy:
    """秘密のRedis URLなしで、固定mount上の期待proxy契約だけを再検証する。"""

    _, body = _load_runtime_configuration(config_path)
    _validate_digest(body, expected_sha256)
    proxy = _parse_configuration(body)
    _validate_proxy_network_contract(proxy, expected_redis_port=None)
    return proxy


def load_runtime_toxiproxy_proxy_snapshot(
    environment: Mapping[str, str],
) -> RuntimeToxiproxyProxySnapshot:
    """seal済みComposeから渡された4値だけをstrict topologyへ変換する。"""

    try:
        snapshot = RuntimeToxiproxyProxySnapshot.model_validate(
            {
                "configuration_sha256": environment[TOXIPROXY_CONFIG_SHA256_ENV],
                "proxy": {
                    "name": environment[TOXIPROXY_EXPECTED_NAME_ENV],
                    "listen": environment[TOXIPROXY_EXPECTED_LISTEN_ENV],
                    "upstream": environment[TOXIPROXY_EXPECTED_UPSTREAM_ENV],
                    "enabled": True,
                },
            },
            strict=True,
        )
    except (KeyError, ValidationError) as error:
        raise AwsDemoDeploymentValidationError("toxiproxy_snapshot_invalid") from error
    _validate_proxy_network_contract(snapshot.proxy, expected_redis_port=None)
    return snapshot


def validate_fault_topology_dns(
    topology: ValidatedFaultTopology,
    validated_host: ValidatedDependencyHost,
) -> None:
    """Redis DNSが唯一の固定upstreamだけを返すことを検証する。"""

    if (
        validated_host.dependency != "redis"
        or validated_host.hostname != topology.redis_hostname
        or validated_host.resolved_addresses != (topology.upstream_address,)
        or validated_host.selected_address != topology.upstream_address
    ):
        _raise("fault_topology_dns_mismatch", "REDIS_URL")


def validate_fault_topology_database_route(
    validated_host: ValidatedDependencyHost,
) -> None:
    """APIの直結fault networkがDB宛てrouteを奪わないことを検証する。"""

    try:
        resolved_addresses = tuple(
            ip_address(address) for address in validated_host.resolved_addresses
        )
    except ValueError:
        has_route_collision = True
    else:
        routed_addresses = (
            address.ipv4_mapped
            if isinstance(address, IPv6Address) and address.ipv4_mapped is not None
            else address
            for address in resolved_addresses
        )
        has_route_collision = any(address in TOXIPROXY_DATA_NETWORK for address in routed_addresses)
    if (
        validated_host.dependency != "database"
        or not validated_host.resolved_addresses
        or validated_host.selected_address not in validated_host.resolved_addresses
        or has_route_collision
    ):
        _raise("fault_topology_database_route_collision", "DATABASE_URL")
