from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from socket import getaddrinfo
from typing import Literal, Protocol

import logfire
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import SettingsError
from redis.exceptions import RedisError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.aws_demo_paths import AwsDemoDeploymentValidationError
from quiz_app.config import (
    AWS_DEMO_OPERATOR_USERNAME_SENTINEL,
    Settings,
)
from quiz_app.contracts import ReadinessHealthResponse, RedisDependencyStatus
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    ValidatedDependencyHost,
    create_pinned_postgres_engine,
    create_pinned_redis_client,
    validate_remote_dependency_host,
)
from quiz_app.health import inspect_readiness
from quiz_app.load_test.fault_topology import (
    load_runtime_fault_topology,
    validate_fault_topology_database_route,
    validate_fault_topology_dns,
)
from quiz_app.operator.auth_service import is_current_operator_password_hash
from quiz_app.participant_id_readiness import inspect_participant_id_readiness
from quiz_app.realtime.redis_adapter import RedisHealthProbe, RedisPublishClient

AWS_DEMO_ENVIRONMENT = "aws-demo"
EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_INVALID_CONFIGURATION = 2
EXIT_DEPENDENCY_ERROR = 3

DependencyName = Literal["database", "redis", "runtime"]
AwsDemoRoundScope = Literal["single-round", "rehearsal-six-round", "load-test-ten-round"]
OutputWriter = Callable[[str], None]
ReadinessInspector = Callable[[Engine, RedisDependencyStatus], ReadinessHealthResponse]


class ClosableRedisClient(RedisPublishClient, Protocol):
    """preflight終了時に明示的に接続資源を解放できるRedis client。"""

    def close(self) -> None: ...


class PreflightConfigurationError(ValueError):
    """秘密値を保持せず、不正だった設定fieldだけを伝える。"""

    def __init__(self, field_names: tuple[str, ...]) -> None:
        self.field_names = field_names
        super().__init__("AWS demo preflight configuration is invalid")


class DependencyInitializationError(RuntimeError):
    """接続URLやdriver例外を公開せず、初期化失敗した依存だけを伝える。"""

    def __init__(self, dependency: DependencyName) -> None:
        self.dependency = dependency
        super().__init__(f"{dependency} initialization failed")


class DeploymentDataReadiness(BaseModel):
    """秘密値や業務dataを含めず、AWS demo用DB準備の判定だけを返す。"""

    status: Literal["ready", "not_ready"]
    event: Literal["ok", "invalid"]
    round_scope: Literal["ok", "invalid"]
    operator: Literal["ok", "invalid"]
    participant_ids: Literal["ok", "invalid", "not_checked"]


class OperatorPreflightRow(BaseModel):
    """DB取得結果を型付き境界へ変換し、想定外の列値をfail-closedにする。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    password_hash: str = Field(min_length=1, max_length=256)
    role: Literal["Operator", "Supervisor"]
    is_demo: bool
    is_revoked: bool


@dataclass
class RuntimeDependencies:
    """preflightだけが所有し、終了時に必ず解放する接続資源。"""

    engine: Engine
    redis_client: ClosableRedisClient

    def close(self) -> tuple[DependencyName, ...]:
        """秘密情報を例外へ展開せず、解放に失敗した依存名を返す。"""

        failed_dependencies: list[DependencyName] = []
        try:
            self.redis_client.close()
        except (RedisError, OSError, RuntimeError):
            failed_dependencies.append("redis")
        try:
            self.engine.dispose()
        except (SQLAlchemyError, OSError):
            failed_dependencies.append("database")
        return tuple(failed_dependencies)


@dataclass(frozen=True, slots=True)
class ValidatedDependencyHosts:
    database: ValidatedDependencyHost
    redis: ValidatedDependencyHost


DataReadinessInspector = Callable[
    [RuntimeDependencies, Settings],
    DeploymentDataReadiness,
]


def _validation_field_names(error: ValidationError) -> tuple[str, ...]:
    """ValidationErrorから入力値やmessageを除外し、field名だけを抽出する。"""

    field_names = {
        str(detail["loc"][0]) if detail["loc"] else "settings"
        for detail in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    }
    return tuple(sorted(field_names))


def load_aws_demo_settings() -> Settings:
    """root `.env` を読まず、process環境変数だけからAWS demo設定を検証する。"""

    try:
        settings = Settings(_env_file=None)
    except ValidationError as error:
        raise PreflightConfigurationError(_validation_field_names(error)) from error
    except SettingsError as error:
        raise PreflightConfigurationError(("settings",)) from error

    if settings.app_env.value != AWS_DEMO_ENVIRONMENT:
        raise PreflightConfigurationError(("app_env",))

    invalid_fields: list[str] = []
    if settings.logfire_send_to_logfire:
        invalid_fields.append("logfire_send_to_logfire")
    if settings.aws_demo_expected_event_id is None:
        invalid_fields.append("aws_demo_expected_event_id")
    if settings.aws_demo_round_scope is None:
        invalid_fields.append("aws_demo_round_scope")
    if (
        settings.aws_demo_round_scope == "load-test-ten-round"
        and settings.aws_demo_toxiproxy_config_sha256 is None
    ):
        invalid_fields.append("aws_demo_toxiproxy_config_sha256")
    if invalid_fields:
        raise PreflightConfigurationError(tuple(invalid_fields))
    return settings


def configure_preflight_logging() -> None:
    """preflightの例外詳細をconsoleやLogfire外部送信へ流さない。"""

    logfire.configure(send_to_logfire=False, console=False)


def create_runtime_dependencies(settings: Settings) -> RuntimeDependencies:
    """検証済みURLから、変更操作を持たないpreflight用clientを作成する。"""

    validated_hosts = validate_resolved_dependency_hosts(settings)
    try:
        engine = create_pinned_postgres_engine(
            settings.database_url,
            validated_hosts.database,
            engine_factory=create_engine,
        )
    except (DependencyHostValidationError, SQLAlchemyError, ValueError) as error:
        raise DependencyInitializationError("database") from error

    try:
        redis_client = create_pinned_redis_client(
            settings.redis_url,
            validated_hosts.redis,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_operation_timeout_seconds,
        )
    except (DependencyHostValidationError, RedisError, ValueError) as error:
        try:
            engine.dispose()
        except (SQLAlchemyError, OSError):
            pass
        raise DependencyInitializationError("redis") from error

    return RuntimeDependencies(engine=engine, redis_client=redis_client)


def validate_resolved_dependency_hosts(settings: Settings) -> ValidatedDependencyHosts:
    """DNS aliasを含むDB/Redis接続先が許可済みremote addressだけへ解決されるか確認する。"""

    validated_hosts: dict[DependencyName, ValidatedDependencyHost] = {}
    for dependency, dependency_url in (
        ("database", settings.database_url),
        ("redis", settings.redis_url),
    ):
        try:
            validated_hosts[dependency] = validate_remote_dependency_host(
                dependency,
                dependency_url,
                resolver=getaddrinfo,
            )
        except DependencyHostValidationError as error:
            raise DependencyInitializationError(dependency) from error
    hosts = ValidatedDependencyHosts(
        database=validated_hosts["database"],
        redis=validated_hosts["redis"],
    )
    if settings.aws_demo_round_scope == "load-test-ten-round":
        try:
            validate_fault_topology_database_route(hosts.database)
        except AwsDemoDeploymentValidationError as error:
            raise DependencyInitializationError("database") from error
        expected_digest = settings.aws_demo_toxiproxy_config_sha256
        if expected_digest is None:
            raise DependencyInitializationError("redis")
        try:
            topology = load_runtime_fault_topology(
                settings.redis_url,
                expected_digest,
            )
            validate_fault_topology_dns(topology, hosts.redis)
        except AwsDemoDeploymentValidationError as error:
            raise DependencyInitializationError("redis") from error
    return hosts


def inspect_deployment_readiness(
    dependencies: RuntimeDependencies,
    readiness_inspector: ReadinessInspector = inspect_readiness,
) -> ReadinessHealthResponse:
    """Redis PING後に既存readinessでSELECTとAlembic revisionだけを確認する。"""

    redis_status = RedisHealthProbe(dependencies.redis_client).status()
    return readiness_inspector(dependencies.engine, redis_status)


def is_deployment_ready(readiness: ReadinessHealthResponse) -> bool:
    """AWS demoを開始できる唯一のreadiness組み合わせか判定する。"""

    return (
        readiness.status == "ready"
        and readiness.database == "ok"
        and readiness.migration == "current"
        and readiness.redis == "ok"
    )


def _is_local_operator_identity(username: str) -> bool:
    """repository既定またはlocal用途と判別できるoperator名を拒否する。"""

    normalized_username = username.casefold()
    return (
        normalized_username
        in {
            AWS_DEMO_OPERATOR_USERNAME_SENTINEL,
            "default-operator",
            "demo-operator",
            "review-operator",
        }
        or normalized_username.startswith(("local-", "local_"))
        or normalized_username.endswith(("@localhost", ".localhost"))
    )


def inspect_deployment_data(
    dependencies: RuntimeDependencies,
    settings: Settings,
) -> DeploymentDataReadiness:
    """唯一EventのID、scope内Round数、専用operatorをread-only queryで確認する。"""

    expected_event_id = settings.aws_demo_expected_event_id
    if expected_event_id is None or settings.aws_demo_round_scope not in {
        "single-round",
        "rehearsal-six-round",
        "load-test-ten-round",
    }:
        raise PreflightConfigurationError(
            (
                "aws_demo_expected_event_id",
                "aws_demo_round_scope",
            )
        )

    with dependencies.engine.connect() as connection:
        event_count = int(connection.scalar(text("SELECT COUNT(*) FROM events")) or 0)
        expected_event_count = int(
            connection.scalar(
                text("SELECT COUNT(*) FROM events WHERE CAST(id AS TEXT) = :event_id"),
                {"event_id": str(expected_event_id)},
            )
            or 0
        )
        round_count = int(
            connection.scalar(
                text("SELECT COUNT(*) FROM quiz_rounds WHERE CAST(event_id AS TEXT) = :event_id"),
                {"event_id": str(expected_event_id)},
            )
            or 0
        )
        question_count = 0
        if settings.aws_demo_round_scope == "rehearsal-six-round":
            question_count = int(
                connection.scalar(
                    text("SELECT COUNT(*) FROM questions WHERE CAST(event_id AS TEXT) = :event_id"),
                    {"event_id": str(expected_event_id)},
                )
                or 0
            )
        raw_operator_rows = (
            connection.execute(
                text(
                    "SELECT username, password_hash, role, is_demo, is_revoked FROM admin_accounts"
                )
            )
            .mappings()
            .all()
        )
        participant_id_status: Literal["ok", "invalid", "not_checked"] = "not_checked"
        if settings.aws_demo_round_scope == "rehearsal-six-round":
            participant_id_status = (
                "ok"
                if inspect_participant_id_readiness(connection, expected_event_id)
                else "invalid"
            )
    operator_rows = [OperatorPreflightRow.model_validate(dict(row)) for row in raw_operator_rows]

    event_status: Literal["ok", "invalid"] = (
        "ok" if event_count == 1 and expected_event_count == 1 else "invalid"
    )
    has_expected_round_count = _round_count_matches_scope(
        settings.aws_demo_round_scope,
        round_count,
        question_count=question_count,
    )
    round_scope_status: Literal["ok", "invalid"] = "ok" if has_expected_round_count else "invalid"
    demo_operator_rows = [row for row in operator_rows if row.is_demo]
    has_local_operator = any(_is_local_operator_identity(row.username) for row in operator_rows)
    has_valid_demo_operator = (
        len(demo_operator_rows) == 1
        and not demo_operator_rows[0].is_revoked
        and demo_operator_rows[0].role == "Operator"
        and is_current_operator_password_hash(demo_operator_rows[0].password_hash)
        and not _is_local_operator_identity(demo_operator_rows[0].username)
    )
    operator_status: Literal["ok", "invalid"] = (
        "ok" if has_valid_demo_operator and not has_local_operator else "invalid"
    )
    is_ready = (
        event_status == "ok"
        and round_scope_status == "ok"
        and operator_status == "ok"
        and participant_id_status != "invalid"
    )
    return DeploymentDataReadiness(
        status="ready" if is_ready else "not_ready",
        event=event_status,
        round_scope=round_scope_status,
        operator=operator_status,
        participant_ids=participant_id_status,
    )


def _round_count_matches_scope(
    round_scope: AwsDemoRoundScope, round_count: int, *, question_count: int = 0
) -> bool:
    """用途別の許容Round数を明示し、過不足をfail-closedにする。"""

    if round_scope == "single-round":
        return round_count == 1
    if round_scope == "rehearsal-six-round":
        # 配置scopeの識別子は維持し、出題数の上限は事前登録した問題数に合わせる。
        return question_count >= 3 and 1 <= round_count <= question_count
    return 1 <= round_count <= 10


def _print_error(message: str) -> None:
    """CLI errorを標準エラーへ出力する。"""

    print(message, file=sys.stderr)


def main(
    *,
    settings_loader: Callable[[], Settings] = load_aws_demo_settings,
    dependency_factory: Callable[[Settings], RuntimeDependencies] = create_runtime_dependencies,
    readiness_inspector: ReadinessInspector = inspect_readiness,
    data_readiness_inspector: DataReadinessInspector = inspect_deployment_data,
    output: OutputWriter = print,
    error_output: OutputWriter = _print_error,
) -> int:
    """安全なreadiness要約だけを出力し、AWS demo配置の可否を終了codeで返す。"""

    configure_preflight_logging()
    try:
        settings = settings_loader()
    except PreflightConfigurationError as error:
        fields = ",".join(error.field_names)
        error_output(f'{{"status":"invalid_configuration","fields":"{fields}"}}')
        return EXIT_INVALID_CONFIGURATION

    try:
        dependencies = dependency_factory(settings)
    except DependencyInitializationError as error:
        error_output(f'{{"status":"dependency_error","dependency":"{error.dependency}"}}')
        return EXIT_DEPENDENCY_ERROR

    readiness: ReadinessHealthResponse | None = None
    data_readiness: DeploymentDataReadiness | None = None
    try:
        readiness = inspect_deployment_readiness(dependencies, readiness_inspector)
        if is_deployment_ready(readiness):
            data_readiness = data_readiness_inspector(dependencies, settings)
    except (SQLAlchemyError, RedisError, OSError, RuntimeError, ValidationError, ValueError):
        error_output('{"status":"dependency_error","dependency":"runtime"}')
    finally:
        cleanup_failures = dependencies.close()

    if readiness is None:
        return EXIT_DEPENDENCY_ERROR
    output(readiness.model_dump_json())
    if data_readiness is not None:
        output(data_readiness.model_dump_json())
    if cleanup_failures:
        dependencies_text = ",".join(cleanup_failures)
        error_output(f'{{"status":"cleanup_error","dependencies":"{dependencies_text}"}}')
        return EXIT_DEPENDENCY_ERROR
    return (
        EXIT_READY
        if is_deployment_ready(readiness)
        and data_readiness is not None
        and data_readiness.status == "ready"
        else EXIT_NOT_READY
    )


if __name__ == "__main__":
    raise SystemExit(main())
