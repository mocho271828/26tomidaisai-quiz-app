from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address
from math import isfinite
from queue import Empty, SimpleQueue
from socket import AF_INET, AF_INET6, SOCK_STREAM, getaddrinfo
from threading import Thread
from typing import Literal, TypeAlias, TypedDict
from urllib.parse import unquote, urlsplit

from redis import Redis as SyncRedis
from redis.connection import SSLConnection
from redis.exceptions import RedisError
from sqlalchemy import Engine, create_engine

from quiz_app.config import is_allowed_aws_remote_address, is_local_service_host

POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
DNS_RESOLUTION_TIMEOUT_SECONDS = 5.0

DependencyName = Literal["database", "redis"]
AddressInfo: TypeAlias = tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]
AddressResolver: TypeAlias = Callable[..., list[AddressInfo]]
DatabaseEngineFactory: TypeAlias = Callable[..., Engine]


class PostgresConnectArgs(TypedDict):
    connect_timeout: int
    host: str
    hostaddr: str


class _PinnedSSLConnection(SSLConnection):
    """接続先だけをliteral IPへ固定し、TLS identityには元hostnameを使う。"""

    def __init__(
        self,
        *,
        host: str,
        pinned_address: str,
        **connection_options: object,
    ) -> None:
        try:
            self._pinned_address = ip_address(pinned_address)
        except ValueError:
            raise RedisError("pinned Redis address is invalid") from None
        super().__init__(host=host, **connection_options)

    def _connect(self) -> socket.socket:
        """resolverを呼ばず、検証済みIPへ直接接続してからTLSで包む。"""

        family = AF_INET if isinstance(self._pinned_address, IPv4Address) else AF_INET6
        endpoint: tuple[str, int] | tuple[str, int, int, int]
        if family == AF_INET:
            endpoint = (str(self._pinned_address), self.port)
        else:
            endpoint = (str(self._pinned_address), self.port, 0, 0)

        sock = socket.socket(family, SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.socket_keepalive:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for option, value in self.socket_keepalive_options.items():
                    sock.setsockopt(socket.IPPROTO_TCP, option, value)
            sock.settimeout(self.socket_connect_timeout)
            sock.connect(endpoint)
            sock.settimeout(self.socket_timeout)
            return self._wrap_socket_with_ssl(sock)
        except (OSError, RedisError):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            raise


@dataclass(frozen=True, slots=True)
class ValidatedDependencyHost:
    dependency: DependencyName
    hostname: str
    resolved_addresses: tuple[str, ...]
    selected_address: str


@dataclass(frozen=True)
class _ResolvedAddressInformation:
    values: tuple[AddressInfo, ...]


@dataclass(frozen=True)
class _AddressResolutionFailure:
    pass


_RESOLUTION_FAILURE = _AddressResolutionFailure()
AddressResolutionOutcome: TypeAlias = _ResolvedAddressInformation | _AddressResolutionFailure


class DependencyHostValidationError(RuntimeError):
    """接続URLや解決先を露出せず、拒否した依存だけを保持する。"""

    def __init__(self, dependency: DependencyName) -> None:
        self.dependency = dependency
        super().__init__(f"{dependency} host validation failed")


def _ordered_allowlisted_addresses(
    dependency: DependencyName,
    addresses: Iterable[str],
) -> tuple[str, ...]:
    try:
        parsed_addresses = {ip_address(address) for address in addresses}
    except ValueError:
        raise DependencyHostValidationError(dependency) from None
    if not parsed_addresses or any(
        not is_allowed_aws_remote_address(address) for address in parsed_addresses
    ):
        raise DependencyHostValidationError(dependency)
    return tuple(
        str(address)
        for address in sorted(
            parsed_addresses,
            key=lambda address: (address.version, int(address)),
        )
    )


def _resolve_with_timeout(
    dependency: DependencyName,
    hostname: str,
    resolver: AddressResolver,
    timeout_seconds: float,
) -> tuple[AddressInfo, ...]:
    """blocking resolverをdaemon threadへ隔離し、呼出元の待機時間を有限にする。"""

    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise DependencyHostValidationError(dependency)

    outcomes: SimpleQueue[AddressResolutionOutcome] = SimpleQueue()

    def resolve() -> None:
        try:
            values = resolver(hostname, None, type=SOCK_STREAM)
        except (OSError, UnicodeError, ValueError, RuntimeError):
            outcomes.put(_RESOLUTION_FAILURE)
            return
        outcomes.put(_ResolvedAddressInformation(tuple(values)))

    worker = Thread(
        target=resolve,
        name=f"{dependency}-host-resolver",
        daemon=True,
    )
    try:
        worker.start()
        outcome = outcomes.get(timeout=timeout_seconds)
    except (Empty, RuntimeError):
        raise DependencyHostValidationError(dependency) from None
    if isinstance(outcome, _AddressResolutionFailure):
        raise DependencyHostValidationError(dependency)
    return outcome.values


def validate_remote_dependency_host(
    dependency: DependencyName,
    dependency_url: str,
    *,
    resolver: AddressResolver = getaddrinfo,
    resolution_timeout_seconds: float = DNS_RESOLUTION_TIMEOUT_SECONDS,
) -> ValidatedDependencyHost:
    """DNS aliasを含む接続先が許可済みremote addressだけへ解決されるか確認する。"""

    try:
        hostname = unquote(urlsplit(dependency_url).hostname or "", errors="strict")
    except (UnicodeError, ValueError):
        raise DependencyHostValidationError(dependency) from None
    address_information = _resolve_with_timeout(
        dependency,
        hostname,
        resolver,
        resolution_timeout_seconds,
    )

    ordered_addresses = _ordered_allowlisted_addresses(
        dependency,
        (
            str(socket_address[0])
            for family, _, _, _, socket_address in address_information
            if family in {AF_INET, AF_INET6}
        ),
    )
    return ValidatedDependencyHost(
        dependency=dependency,
        hostname=hostname,
        resolved_addresses=ordered_addresses,
        selected_address=ordered_addresses[0],
    )


def create_pinned_postgres_engine(
    database_url: str,
    validated_host: ValidatedDependencyHost,
    *,
    engine_factory: DatabaseEngineFactory = create_engine,
) -> Engine:
    """verify-full用hostnameを維持し、接続先を検証済みIPへ固定する。"""

    try:
        database_hostname = unquote(urlsplit(database_url).hostname or "", errors="strict")
    except (UnicodeError, ValueError):
        raise DependencyHostValidationError("database") from None
    ordered_addresses = _ordered_allowlisted_addresses(
        "database",
        validated_host.resolved_addresses,
    )
    if (
        validated_host.dependency != "database"
        or not database_hostname
        or database_hostname != validated_host.hostname
        or ordered_addresses != validated_host.resolved_addresses
        or validated_host.selected_address != ordered_addresses[0]
    ):
        raise DependencyHostValidationError("database")

    connect_args = PostgresConnectArgs(
        connect_timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
        host=validated_host.hostname,
        hostaddr=validated_host.selected_address,
    )
    return engine_factory(
        database_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args=connect_args,
    )


def create_pinned_redis_client(
    redis_url: str,
    validated_host: ValidatedDependencyHost,
    *,
    decode_responses: bool,
    socket_connect_timeout: float,
    socket_timeout: float,
) -> SyncRedis:
    """検証済みIPへRedis TLS接続を固定し、元FQDNで証明書を検証する。"""

    try:
        parsed_url = urlsplit(redis_url)
        redis_hostname = unquote(parsed_url.hostname or "", errors="strict")
        parsed_port = parsed_url.port
    except (UnicodeError, ValueError):
        raise DependencyHostValidationError("redis") from None

    ordered_addresses = _ordered_allowlisted_addresses(
        "redis",
        validated_host.resolved_addresses,
    )
    if (
        not redis_url.isascii()
        or parsed_url.scheme != "rediss"
        or not redis_hostname
        or is_local_service_host(redis_hostname)
        or parsed_url.query
        or parsed_url.fragment
        or parsed_port == 0
        or validated_host.dependency != "redis"
        or redis_hostname != validated_host.hostname
        or ordered_addresses != validated_host.resolved_addresses
        or validated_host.selected_address != ordered_addresses[0]
        or not isfinite(socket_connect_timeout)
        or socket_connect_timeout <= 0
        or not isfinite(socket_timeout)
        or socket_timeout <= 0
    ):
        raise DependencyHostValidationError("redis")

    try:
        return SyncRedis.from_url(
            redis_url,
            connection_class=_PinnedSSLConnection,
            pinned_address=validated_host.selected_address,
            decode_responses=decode_responses,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
            ssl_cert_reqs="required",
            ssl_check_hostname=True,
        )
    except (RedisError, ValueError):
        raise DependencyHostValidationError("redis") from None
