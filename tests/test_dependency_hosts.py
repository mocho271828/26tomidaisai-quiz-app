import socket
import ssl
from socket import AF_INET, SOCK_STREAM
from threading import Event, current_thread
from time import monotonic
from typing import cast
from uuid import uuid4

import pytest
from redis import connection as redis_connection
from redis.connection import SSLConnection
from sqlalchemy import Engine

from quiz_app import dependency_hosts as dependency_hosts_module
from quiz_app.dependency_hosts import (
    AddressInfo,
    DependencyHostValidationError,
    ValidatedDependencyHost,
    create_pinned_postgres_engine,
    create_pinned_redis_client,
    validate_remote_dependency_host,
)


def _remote_address_information() -> list[AddressInfo]:
    return [(AF_INET, SOCK_STREAM, 6, "", ("10.0.0.8", 0))]


def test_dependency_host_resolver_runs_in_daemon_thread() -> None:
    daemon_states: list[bool] = []

    def resolve_remote(
        _hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[AddressInfo]:
        assert type == SOCK_STREAM
        daemon_states.append(current_thread().daemon)
        return _remote_address_information()

    validated_host = validate_remote_dependency_host(
        "database",
        "postgresql+psycopg://db.aws.internal/load",
        resolver=resolve_remote,
    )

    assert daemon_states == [True]
    assert validated_host.hostname == "db.aws.internal"
    assert validated_host.selected_address == "10.0.0.8"


def test_dependency_host_resolution_timeout_returns_secret_free_refusal() -> None:
    release_resolver = Event()
    secret_marker = f"private-dns-{uuid4()}"

    def block_resolution(
        _hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[AddressInfo]:
        assert type == SOCK_STREAM
        release_resolver.wait(timeout=1)
        return _remote_address_information()

    started_at = monotonic()
    try:
        with pytest.raises(DependencyHostValidationError) as error_info:
            validate_remote_dependency_host(
                "database",
                f"postgresql+psycopg://user:{secret_marker}@db.aws.internal/load",
                resolver=block_resolution,
                resolution_timeout_seconds=0.02,
            )
    finally:
        release_resolver.set()

    assert monotonic() - started_at < 0.5
    assert secret_marker not in str(error_info.value)
    assert error_info.value.__cause__ is None


def test_multiple_addresses_are_sorted_and_pinned_without_second_resolution() -> None:
    resolver_calls = 0

    def resolve_multiple(
        _hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[AddressInfo]:
        nonlocal resolver_calls
        assert type == SOCK_STREAM
        resolver_calls += 1
        return [
            (AF_INET, SOCK_STREAM, 6, "", ("10.0.0.9", 0)),
            (AF_INET, SOCK_STREAM, 6, "", ("10.0.0.8", 0)),
            (AF_INET, SOCK_STREAM, 6, "", ("10.0.0.9", 0)),
        ]

    database_url = "postgresql+psycopg://user:password@db.aws.internal:5432/load"
    validated_host = validate_remote_dependency_host(
        "database",
        database_url,
        resolver=resolve_multiple,
    )
    engine_options: dict[str, object] = {}

    def capture_engine(_url: str, **options: object) -> Engine:
        engine_options.update(options)
        return cast(Engine, object())

    create_pinned_postgres_engine(
        database_url,
        validated_host,
        engine_factory=capture_engine,
    )

    assert resolver_calls == 1
    assert validated_host.resolved_addresses == ("10.0.0.8", "10.0.0.9")
    assert validated_host.selected_address == "10.0.0.8"
    assert engine_options == {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {
            "connect_timeout": 5,
            "host": "db.aws.internal",
            "hostaddr": "10.0.0.8",
        },
    }


@pytest.mark.parametrize(
    "address_information",
    [
        [],
        [
            (AF_INET, SOCK_STREAM, 6, "", ("10.0.0.8", 0)),
            (AF_INET, SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ],
    ],
)
def test_empty_or_mixed_unsafe_dns_response_is_rejected(
    address_information: list[AddressInfo],
) -> None:
    def resolve_addresses(
        _hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[AddressInfo]:
        assert type == SOCK_STREAM
        return address_information

    with pytest.raises(DependencyHostValidationError):
        validate_remote_dependency_host(
            "database",
            "postgresql+psycopg://db.aws.internal/load",
            resolver=resolve_addresses,
        )


def test_pinned_engine_revalidates_forged_host_result() -> None:
    forged_host = ValidatedDependencyHost(
        dependency="database",
        hostname="db.aws.internal",
        resolved_addresses=("10.0.0.8", "127.0.0.1"),
        selected_address="10.0.0.8",
    )

    with pytest.raises(DependencyHostValidationError):
        create_pinned_postgres_engine(
            "postgresql+psycopg://db.aws.internal/load",
            forged_host,
            engine_factory=lambda *_args, **_kwargs: pytest.fail(
                "不正な検証結果でEngineを作成してはいけません"
            ),
        )


@pytest.mark.parametrize(
    ("selected_address", "expected_family", "expected_endpoint"),
    [
        ("10.0.0.8", socket.AF_INET, ("10.0.0.8", 6380)),
        ("fd00::8", socket.AF_INET6, ("fd00::8", 6380, 0, 0)),
    ],
)
def test_pinned_redis_client_uses_selected_ip_and_original_tls_hostname_without_dns(
    monkeypatch: pytest.MonkeyPatch,
    selected_address: str,
    expected_family: int,
    expected_endpoint: tuple[str, int] | tuple[str, int, int, int],
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.connected_endpoint: tuple[str, int] | tuple[str, int, int, int] | None = None
            self.timeouts: list[float | None] = []
            self.is_closed = False

        def setsockopt(self, _level: int, _option: int, _value: int | bytes) -> None:
            return None

        def settimeout(self, timeout: float | None) -> None:
            self.timeouts.append(timeout)

        def connect(
            self,
            endpoint: tuple[str, int] | tuple[str, int, int, int],
        ) -> None:
            self.connected_endpoint = endpoint

        def shutdown(self, _how: int) -> None:
            return None

        def close(self) -> None:
            self.is_closed = True

    class FakeSslContext:
        def __init__(self) -> None:
            self.check_hostname = False
            self.verify_mode = ssl.CERT_NONE
            self.server_hostname: str | None = None

        def wrap_socket(
            self,
            raw_socket: socket.socket,
            *,
            server_hostname: str,
        ) -> socket.socket:
            self.server_hostname = server_hostname
            return raw_socket

    created_socket = FakeSocket()
    created_context = FakeSslContext()

    def reject_dns(*_args: object, **_kwargs: object) -> list[AddressInfo]:
        pytest.fail("検証済みRedis接続でDNSを再実行してはいけません")

    def create_socket(family: int, socket_type: int) -> socket.socket:
        assert family == expected_family
        assert socket_type == SOCK_STREAM
        return cast(socket.socket, created_socket)

    monkeypatch.setattr(dependency_hosts_module.socket, "getaddrinfo", reject_dns)
    monkeypatch.setattr(dependency_hosts_module.socket, "socket", create_socket)
    monkeypatch.setattr(
        redis_connection.ssl,
        "create_default_context",
        lambda: created_context,
    )
    validated_host = ValidatedDependencyHost(
        dependency="redis",
        hostname="cache.aws.internal",
        resolved_addresses=(selected_address,),
        selected_address=selected_address,
    )
    client = create_pinned_redis_client(
        "rediss://load-user:private-value@cache.aws.internal:6380/0",
        validated_host,
        decode_responses=True,
        socket_connect_timeout=0.2,
        socket_timeout=0.3,
    )
    connection = client.connection_pool.make_connection()

    assert isinstance(connection, SSLConnection)
    assert connection.host == "cache.aws.internal"
    assert connection.cert_reqs == ssl.CERT_REQUIRED
    assert connection.check_hostname is True
    assert connection._connect() is created_socket
    assert created_socket.connected_endpoint == expected_endpoint
    assert created_socket.timeouts == [0.2, 0.3]
    assert created_context.check_hostname is True
    assert created_context.verify_mode == ssl.CERT_REQUIRED
    assert created_context.server_hostname == "cache.aws.internal"
    assert created_socket.is_closed is False


def test_pinned_redis_client_rejects_forged_identity_without_secret_leak() -> None:
    secret_marker = f"private-redis-{uuid4()}"
    validated_host = ValidatedDependencyHost(
        dependency="redis",
        hostname="cache.aws.internal",
        resolved_addresses=("10.0.0.8",),
        selected_address="10.0.0.8",
    )

    with pytest.raises(DependencyHostValidationError) as error_info:
        create_pinned_redis_client(
            f"rediss://load-user:{secret_marker}@different.aws.internal:6380/0",
            validated_host,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.3,
        )

    assert error_info.value.dependency == "redis"
    assert secret_marker not in str(error_info.value)
    assert error_info.value.__cause__ is None


def test_pinned_redis_client_revalidates_selected_address_order() -> None:
    forged_host = ValidatedDependencyHost(
        dependency="redis",
        hostname="cache.aws.internal",
        resolved_addresses=("10.0.0.8", "10.0.0.9"),
        selected_address="10.0.0.9",
    )

    with pytest.raises(DependencyHostValidationError):
        create_pinned_redis_client(
            "rediss://load-user:private-value@cache.aws.internal:6380/0",
            forged_host,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.3,
        )


def test_dependency_host_known_resolver_error_is_secret_free() -> None:
    secret_marker = f"private-resolver-{uuid4()}"

    def fail_resolution(
        _hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[AddressInfo]:
        assert type == SOCK_STREAM
        raise OSError(f"resolver leaked {secret_marker}")

    with pytest.raises(DependencyHostValidationError) as error_info:
        validate_remote_dependency_host(
            "redis",
            "rediss://user:secret@cache.aws.internal/0",
            resolver=fail_resolution,
            resolution_timeout_seconds=0.1,
        )

    assert secret_marker not in str(error_info.value)
    assert error_info.value.__cause__ is None
