import base64
import secrets
from pathlib import Path
from socket import AF_INET, SOCK_STREAM
from typing import cast

import pytest
from cryptography.fernet import Fernet
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError

from quiz_app import deployment_preflight
from quiz_app.config import Settings
from quiz_app.contracts import ReadinessHealthResponse
from quiz_app.dependency_hosts import ValidatedDependencyHost
from quiz_app.deployment_preflight import (
    EXIT_DEPENDENCY_ERROR,
    EXIT_INVALID_CONFIGURATION,
    EXIT_NOT_READY,
    EXIT_READY,
    AwsDemoRoundScope,
    DeploymentDataReadiness,
    PreflightConfigurationError,
    RuntimeDependencies,
    ValidatedDependencyHosts,
    _round_count_matches_scope,
    configure_preflight_logging,
    load_aws_demo_settings,
    main,
    validate_resolved_dependency_hosts,
)
from quiz_app.schema_revision import SCHEMA_REVISION

SENSITIVE_VALUE_MARKER = "must-not-appear-super-secret"
DATABASE_URL_MARKER = f"postgresql+psycopg://user:{SENSITIVE_VALUE_MARKER}@db.example.test/quiz"


class RecordingRedisClient:
    def __init__(self, *, is_available: bool = True) -> None:
        self.is_available = is_available
        self.ping_count = 0
        self.close_count = 0
        self.publish_count = 0

    def publish(self, channel: str, message: str) -> int:
        del channel, message
        self.publish_count += 1
        return 1

    def ping(self) -> bool:
        self.ping_count += 1
        return self.is_available

    def close(self) -> None:
        self.close_count += 1


class FailingRedisClient(RecordingRedisClient):
    def ping(self) -> bool:
        self.ping_count += 1
        raise RedisConnectionError(
            f"cannot connect to rediss://user:{SENSITIVE_VALUE_MARKER}@cache.example.test/0"
        )


def _clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
        monkeypatch.delenv(field_name, raising=False)


def _set_valid_aws_demo_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_settings_environment(monkeypatch)
    database_credential = f"Aa0_{secrets.token_urlsafe(32)}"
    redis_credential = f"Aa0_{secrets.token_urlsafe(32)}"
    session_secret = f"Aa0_{secrets.token_urlsafe(32)}"
    environment = {
        "APP_ENV": "aws-demo",
        "APP_VERSION": "0123456789abcdef0123456789abcdef01234567",
        "DATABASE_URL": (
            f"postgresql+psycopg://aws_demo:{database_credential}@"
            "db.aws.internal:5432/quiz?sslmode=verify-full"
        ),
        "REDIS_URL": f"rediss://aws_demo:{redis_credential}@redis.aws.internal:6380/0",
        "DEMO_OPERATOR_USERNAME": "managed-in-database",
        "DEMO_OPERATOR_PASSWORD": "unused",
        "SESSION_SECRET": session_secret,
        "ENABLE_DEMO_AUTH": "true",
        "ENABLE_DEMO_SEED": "false",
        "SESSION_COOKIE_SECURE": "true",
        "OPERATOR_SESSION_TTL_MINUTES": "120",
        "PARTICIPANT_SESSION_TTL_MINUTES": "120",
        "REALTIME_ALLOWED_ORIGINS": '["https://quiz-demo.aws.internal"]',
        "API_INSTANCE_ID": "api-single",
        "PGSSLROOTCERT": "/run/secrets/aws-demo-postgres-ca.pem",
        "LOGFIRE_SEND_TO_LOGFIRE": "false",
        "ROUND_BACKUP_STORAGE_MODE": "disabled",
        "ROUND_BACKUP_DIRECTORY": "/var/empty/quiz-round-backups",
        "ROUND_BACKUP_ENCRYPTION_KEY": "disabled",
        "AWS_DEMO_EXPECTED_EVENT_ID": "70000000-0000-4000-8000-000000000001",
        "AWS_DEMO_ROUND_SCOPE": "single-round",
    }
    for field_name, value in environment.items():
        monkeypatch.setenv(field_name, value)


def _runtime_dependencies(
    revision: str,
    redis_client: RecordingRedisClient | None = None,
) -> tuple[RuntimeDependencies, RecordingRedisClient]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("CREATE TABLE events (id TEXT PRIMARY KEY)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )
        connection.execute(text("INSERT INTO events (id) VALUES ('event-1')"))
    client = redis_client or RecordingRedisClient()
    return RuntimeDependencies(engine=engine, redis_client=client), client


def _run_main(dependencies: RuntimeDependencies) -> tuple[int, list[str], list[str]]:
    standard_output: list[str] = []
    error_output: list[str] = []
    exit_code = main(
        settings_loader=lambda: cast(Settings, object()),
        dependency_factory=lambda _: dependencies,
        data_readiness_inspector=lambda *_: DeploymentDataReadiness(
            status="ready",
            event="ok",
            round_scope="ok",
            operator="ok",
            participant_ids="ok",
        ),
        output=standard_output.append,
        error_output=error_output.append,
    )
    return exit_code, standard_output, error_output


def test_ready_dependencies_are_the_only_success_result() -> None:
    dependencies, redis_client = _runtime_dependencies(SCHEMA_REVISION)
    database_statements: list[str] = []

    def record_database_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        database_statements.append(statement)

    event.listen(dependencies.engine, "before_cursor_execute", record_database_statement)

    exit_code, standard_output, error_output = _run_main(dependencies)

    assert exit_code == EXIT_READY
    assert ReadinessHealthResponse.model_validate_json(standard_output[0]) == (
        ReadinessHealthResponse(
            status="ready",
            database="ok",
            migration="current",
            redis="ok",
        )
    )
    assert DeploymentDataReadiness.model_validate_json(standard_output[1]).status == "ready"
    assert error_output == []
    assert any(statement.strip().upper() == "SELECT 1" for statement in database_statements)
    assert any("alembic_version" in statement for statement in database_statements)
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "PRAGMA"))
        for statement in database_statements
    )
    assert redis_client.ping_count == 1
    assert redis_client.publish_count == 0
    assert redis_client.close_count == 1


def test_redis_degradation_fails_preflight_without_exposing_connection_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies, redis_client = _runtime_dependencies(
        SCHEMA_REVISION,
        FailingRedisClient(),
    )

    exit_code, standard_output, error_output = _run_main(dependencies)

    readiness = ReadinessHealthResponse.model_validate_json(standard_output[0])
    assert exit_code == EXIT_NOT_READY
    assert readiness.status == "degraded"
    assert readiness.database == "ok"
    assert readiness.migration == "current"
    assert readiness.redis == "degraded"
    assert error_output == []
    assert SENSITIVE_VALUE_MARKER not in "".join(standard_output + error_output)
    captured = capsys.readouterr()
    assert SENSITIVE_VALUE_MARKER not in captured.out + captured.err
    assert redis_client.ping_count == 1
    assert redis_client.publish_count == 0
    assert redis_client.close_count == 1


def test_outdated_schema_fails_preflight() -> None:
    dependencies, redis_client = _runtime_dependencies("older_revision")

    exit_code, standard_output, error_output = _run_main(dependencies)

    readiness = ReadinessHealthResponse.model_validate_json(standard_output[0])
    assert exit_code == EXIT_NOT_READY
    assert readiness.status == "not_ready"
    assert readiness.database == "ok"
    assert readiness.migration == "outdated"
    assert readiness.redis == "ok"
    assert error_output == []
    assert redis_client.ping_count == 1
    assert redis_client.publish_count == 0


def test_invalid_deployment_data_fails_preflight() -> None:
    dependencies, redis_client = _runtime_dependencies(SCHEMA_REVISION)
    standard_output: list[str] = []
    error_output: list[str] = []

    exit_code = main(
        settings_loader=lambda: cast(Settings, object()),
        dependency_factory=lambda _: dependencies,
        data_readiness_inspector=lambda *_: DeploymentDataReadiness(
            status="not_ready",
            event="invalid",
            round_scope="invalid",
            operator="invalid",
            participant_ids="invalid",
        ),
        output=standard_output.append,
        error_output=error_output.append,
    )

    assert exit_code == EXIT_NOT_READY
    assert ReadinessHealthResponse.model_validate_json(standard_output[0]).status == "ready"
    assert DeploymentDataReadiness.model_validate_json(standard_output[1]).status == "not_ready"
    assert error_output == []
    assert redis_client.close_count == 1


def test_non_aws_demo_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "local-demo")
    monkeypatch.setenv("ROUND_BACKUP_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    with pytest.raises(PreflightConfigurationError) as raised:
        load_aws_demo_settings()

    assert raised.value.field_names == ("app_env",)


def test_aws_demo_disables_external_logfire_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)

    settings = load_aws_demo_settings()

    assert settings.app_env.value == "aws-demo"
    assert settings.logfire_send_to_logfire is False

    monkeypatch.setenv("LOGFIRE_SEND_TO_LOGFIRE", "true")
    with pytest.raises(PreflightConfigurationError) as raised:
        load_aws_demo_settings()

    assert raised.value.field_names == ("settings",)


def test_aws_demo_preflight_requires_release_event_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    monkeypatch.delenv("AWS_DEMO_EXPECTED_EVENT_ID")

    with pytest.raises(PreflightConfigurationError) as raised:
        load_aws_demo_settings()

    assert raised.value.field_names == ("aws_demo_expected_event_id",)


@pytest.mark.parametrize(
    ("round_scope", "round_count", "expected"),
    [
        ("single-round", 0, False),
        ("single-round", 1, True),
        ("single-round", 2, False),
        ("load-test-ten-round", 0, False),
        ("load-test-ten-round", 1, True),
        ("load-test-ten-round", 10, True),
        ("load-test-ten-round", 11, False),
    ],
)
def test_preflight_round_count_is_bounded_by_release_scope(
    round_scope: str,
    round_count: int,
    expected: bool,
) -> None:
    assert _round_count_matches_scope(cast(AwsDemoRoundScope, round_scope), round_count) is expected


def test_settings_never_load_implicit_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "APP_ENV=local-demo\nLOGFIRE_SEND_TO_LOGFIRE=true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV", "local-demo")
    monkeypatch.setenv("ROUND_BACKUP_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("LOGFIRE_SEND_TO_LOGFIRE", "false")

    with pytest.raises(PreflightConfigurationError) as raised:
        load_aws_demo_settings()

    assert raised.value.field_names == ("app_env",)


def test_validation_error_output_contains_only_field_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_settings_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "local-demo")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL_MARKER)
    monkeypatch.setenv("SESSION_SECRET", SENSITIVE_VALUE_MARKER)
    monkeypatch.setenv("OPERATOR_SESSION_TTL_MINUTES", SENSITIVE_VALUE_MARKER)

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == EXIT_INVALID_CONFIGURATION
    assert captured.out == ""
    assert "operator_session_ttl_minutes" in captured.err
    assert SENSITIVE_VALUE_MARKER not in captured.err
    assert DATABASE_URL_MARKER not in captured.err
    assert "Input should be" not in captured.err


def test_dependency_initialization_error_drops_driver_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    settings = load_aws_demo_settings()

    create_engine_options: dict[str, object] = {}

    def fail_create_engine(*_: object, **options: object) -> None:
        create_engine_options.update(options)
        raise SQLAlchemyError(f"failed to connect to {DATABASE_URL_MARKER}")

    monkeypatch.setattr(deployment_preflight, "create_engine", fail_create_engine)
    monkeypatch.setattr(
        deployment_preflight,
        "validate_resolved_dependency_hosts",
        lambda _: ValidatedDependencyHosts(
            database=ValidatedDependencyHost(
                dependency="database",
                hostname="db.aws.internal",
                resolved_addresses=("10.0.0.8",),
                selected_address="10.0.0.8",
            ),
            redis=ValidatedDependencyHost(
                dependency="redis",
                hostname="redis.aws.internal",
                resolved_addresses=("10.0.0.9",),
                selected_address="10.0.0.9",
            ),
        ),
    )

    exit_code = main(settings_loader=lambda: settings)

    captured = capsys.readouterr()
    assert exit_code == EXIT_DEPENDENCY_ERROR
    assert captured.out == ""
    assert '"dependency":"database"' in captured.err
    assert SENSITIVE_VALUE_MARKER not in captured.err
    assert DATABASE_URL_MARKER not in captured.err
    assert create_engine_options["connect_args"] == {
        "connect_timeout": 5,
        "host": "db.aws.internal",
        "hostaddr": "10.0.0.8",
    }


def test_runtime_dependencies_pin_redis_ip_and_keep_validated_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    settings = load_aws_demo_settings()
    validated_hosts = ValidatedDependencyHosts(
        database=ValidatedDependencyHost(
            dependency="database",
            hostname="db.aws.internal",
            resolved_addresses=("10.0.0.8",),
            selected_address="10.0.0.8",
        ),
        redis=ValidatedDependencyHost(
            dependency="redis",
            hostname="redis.aws.internal",
            resolved_addresses=("10.0.0.9",),
            selected_address="10.0.0.9",
        ),
    )
    fake_engine = cast(Engine, object())
    fake_redis = RecordingRedisClient()
    redis_calls: list[tuple[str, ValidatedDependencyHost, dict[str, object]]] = []

    monkeypatch.setattr(
        deployment_preflight,
        "validate_resolved_dependency_hosts",
        lambda _: validated_hosts,
    )
    monkeypatch.setattr(
        deployment_preflight,
        "create_pinned_postgres_engine",
        lambda *_args, **_kwargs: fake_engine,
    )

    def create_redis(
        redis_url: str,
        validated_host: ValidatedDependencyHost,
        **options: object,
    ) -> RecordingRedisClient:
        redis_calls.append((redis_url, validated_host, options))
        return fake_redis

    monkeypatch.setattr(deployment_preflight, "create_pinned_redis_client", create_redis)

    dependencies = deployment_preflight.create_runtime_dependencies(settings)

    assert dependencies.engine is fake_engine
    assert dependencies.redis_client is fake_redis
    assert redis_calls == [
        (
            settings.redis_url,
            validated_hosts.redis,
            {
                "decode_responses": True,
                "socket_connect_timeout": settings.redis_connect_timeout_seconds,
                "socket_timeout": settings.redis_operation_timeout_seconds,
            },
        )
    ]


def test_resolved_dependency_host_rejects_loopback_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    settings = load_aws_demo_settings()

    def resolve_to_loopback(
        hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del hostname
        assert type == SOCK_STREAM
        return [(AF_INET, SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(deployment_preflight, "getaddrinfo", resolve_to_loopback)

    with pytest.raises(
        deployment_preflight.DependencyInitializationError,
        match="database initialization failed",
    ):
        validate_resolved_dependency_hosts(settings)


def test_load_preflight_requires_fault_topology_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    monkeypatch.setenv("AWS_DEMO_ROUND_SCOPE", "load-test-ten-round")
    monkeypatch.setenv("ROUND_BACKUP_STORAGE_MODE", "filesystem")
    monkeypatch.setenv("ROUND_BACKUP_DIRECTORY", "/var/lib/quiz-round-backups")
    monkeypatch.setenv(
        "ROUND_BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii"),
    )

    with pytest.raises(PreflightConfigurationError) as raised:
        load_aws_demo_settings()

    assert raised.value.field_names == ("aws_demo_toxiproxy_config_sha256",)


def test_load_dependency_validation_binds_proxy_upstream_to_container_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    settings = load_aws_demo_settings().model_copy(
        update={
            "aws_demo_round_scope": "load-test-ten-round",
            "aws_demo_toxiproxy_config_sha256": "a1" * 32,
        }
    )

    def resolve_remote(
        hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert type == SOCK_STREAM
        address = "10.0.1.15" if hostname == "db.aws.internal" else "10.0.2.15"
        return [(AF_INET, SOCK_STREAM, 6, "", (address, 0))]

    topology = cast(object, {"validated": True})
    calls: list[tuple[object, ValidatedDependencyHost]] = []
    monkeypatch.setattr(deployment_preflight, "getaddrinfo", resolve_remote)
    monkeypatch.setattr(
        deployment_preflight,
        "load_runtime_fault_topology",
        lambda redis_url, digest: (
            topology
            if redis_url == settings.redis_url and digest == "a1" * 32
            else pytest.fail("unexpected fault topology input")
        ),
    )
    monkeypatch.setattr(
        deployment_preflight,
        "validate_fault_topology_dns",
        lambda loaded, redis_host: calls.append((loaded, redis_host)),
    )

    hosts = validate_resolved_dependency_hosts(settings)

    assert hosts.redis.selected_address == "10.0.2.15"
    assert calls == [(topology, hosts.redis)]


def test_load_dependency_validation_rejects_database_route_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_aws_demo_environment(monkeypatch)
    settings = load_aws_demo_settings().model_copy(
        update={
            "aws_demo_round_scope": "load-test-ten-round",
            "aws_demo_toxiproxy_config_sha256": "a1" * 32,
        }
    )

    def resolve_remote(
        hostname: str,
        _port: object,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert type == SOCK_STREAM
        address = "172.30.250.3" if hostname == "db.aws.internal" else "10.0.2.15"
        return [(AF_INET, SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(deployment_preflight, "getaddrinfo", resolve_remote)

    with pytest.raises(
        deployment_preflight.DependencyInitializationError,
        match="database initialization failed",
    ):
        validate_resolved_dependency_hosts(settings)


def test_unexpected_readiness_error_is_redacted() -> None:
    dependencies, _ = _runtime_dependencies(SCHEMA_REVISION)
    standard_output: list[str] = []
    error_output: list[str] = []

    def fail_readiness(*_: object) -> ReadinessHealthResponse:
        raise RuntimeError(f"driver failed with {SENSITIVE_VALUE_MARKER}")

    exit_code = main(
        settings_loader=lambda: cast(Settings, object()),
        dependency_factory=lambda _: dependencies,
        readiness_inspector=fail_readiness,
        output=standard_output.append,
        error_output=error_output.append,
    )

    assert exit_code == EXIT_DEPENDENCY_ERROR
    assert standard_output == []
    assert error_output == ['{"status":"dependency_error","dependency":"runtime"}']
    assert SENSITIVE_VALUE_MARKER not in "".join(error_output)


def test_logfire_is_never_configured_for_external_or_console_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_calls: list[dict[str, object]] = []

    def record_configure(**kwargs: object) -> None:
        configure_calls.append(kwargs)

    monkeypatch.setattr(deployment_preflight.logfire, "configure", record_configure)

    configure_preflight_logging()

    assert configure_calls == [{"send_to_logfire": False, "console": False}]
