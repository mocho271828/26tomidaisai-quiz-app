"""人が新規専用DBに一度だけ実行する、既存migrationと最小権限の初期化。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self

import logfire
from psycopg import sql
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel

from quiz_app.bootstrap.demo_seed import (
    DEMO_CREDENTIAL_POLICY_VERSION,
    DEMO_DISPLAY_NAME_POLICY_VERSION,
)
from quiz_app.config import Settings, is_strong_aws_demo_secret, is_valid_aws_demo_database_url
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    PostgresConnectArgs,
    ValidatedDependencyHost,
    create_pinned_postgres_engine,
    validate_remote_dependency_host,
)
from quiz_app.models import AdminAccount, DisplayNamePolicy
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import create_operator_password_hasher
from quiz_app.quiz_configuration import configured_scoring_policy, get_quiz_settings
from quiz_app.rehearsal.preparation import (
    PREPARATION_LOCK_KEY,
    RehearsalEventInput,
    RehearsalPreparationRefusedError,
    _dispose_engine,
    _require_current_schema,
    validate_rehearsal_settings,
)

APPLICATION_ROLE = "rehearsal_app"
IMMUTABLE_PARTICIPANT_ID_TABLES = frozenset({"participant_id_pools", "participant_id_slots"})
SCORING_POLICY_VERSION = get_quiz_settings().scoring.version


class RehearsalInitializationSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    admin_database_url: SecretStr
    app_database_url: SecretStr
    operator_username: str
    operator_password: SecretStr

    @field_validator("admin_database_url", "app_database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not is_valid_aws_demo_database_url(value.get_secret_value()):
            raise ValueError("rehearsal_initialization_database_url_invalid")
        return value

    @field_validator("operator_username")
    @classmethod
    def validate_operator_username(cls, value: str) -> str:
        if value != "rehearsal-operator":
            raise ValueError("rehearsal_initialization_operator_name_invalid")
        return value

    @field_validator("operator_password")
    @classmethod
    def validate_operator_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if len(password) > 128 or not is_strong_aws_demo_secret(password, minimum_length=32):
            raise ValueError("rehearsal_initialization_operator_password_invalid")
        return value

    @model_validator(mode="after")
    def validate_matching_database(self) -> Self:
        admin = make_url(self.admin_database_url.get_secret_value())
        app = make_url(self.app_database_url.get_secret_value())
        if (
            (admin.host, admin.port, admin.database) != (app.host, app.port, app.database)
            or app.username != APPLICATION_ROLE
            or admin.username == app.username
        ):
            raise ValueError("rehearsal_initialization_database_identity_mismatch")
        return self


class _DatabaseIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    database_name: str
    current_role: str
    session_role: str


class _Presence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    exists: bool


def _require_admin_identity(session: Session, secrets: RehearsalInitializationSecrets) -> None:
    row = (
        session.execute(
            text(
                "SELECT current_database() AS database_name, current_user AS current_role, "
                "session_user AS session_role"
            )
        )
        .mappings()
        .one()
    )
    identity = _DatabaseIdentity.model_validate(dict(row))
    expected = make_url(secrets.admin_database_url.get_secret_value())
    if (
        identity.database_name != expected.database
        or identity.current_role != expected.username
        or identity.session_role != expected.username
    ):
        raise RehearsalPreparationRefusedError("rehearsal_initialization_admin_identity_mismatch")
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SET LOCAL statement_timeout = '30s'"))
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PREPARATION_LOCK_KEY})
    role_exists = _Presence.model_validate(
        {
            "exists": session.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :role)"),
                {"role": APPLICATION_ROLE},
            )
        }
    )
    if role_exists.exists:
        raise RehearsalPreparationRefusedError("rehearsal_initialization_role_already_exists")


def _require_fresh_database(session: Session) -> None:
    # RDS既定public schemaは許し、既存migration/開催/その他tableの再利用は拒否する。
    exists = session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE')"
        )
    )
    if _Presence.model_validate({"exists": exists}).exists:
        raise RehearsalPreparationRefusedError("rehearsal_initialization_database_not_fresh")


def _migration_environment(settings: Settings, secrets: RehearsalInitializationSecrets) -> str:
    lines: list[str] = []
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if value is None:
            continue
        if name == "database_url":
            value = secrets.admin_database_url.get_secret_value()
        elif isinstance(value, SecretStr):
            value = value.get_secret_value()
        elif isinstance(value, bool):
            value = str(value).lower()
        elif name == "realtime_allowed_origins":
            value = json.dumps([str(origin) for origin in settings.realtime_allowed_origins])
        key = "PGSSLROOTCERT" if name == "postgres_ssl_root_cert" else name.upper()
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def run_existing_migrations(
    repository_root: Path,
    settings: Settings,
    secrets: RehearsalInitializationSecrets,
    postgres_ca_file: Path,
    host: ValidatedDependencyHost,
) -> None:
    """既存env/version fileを変更せず、専用dotenvとlibpq serviceで人手migrationを実行する。"""
    if any(character in str(postgres_ca_file) for character in "\r\n"):
        raise RehearsalPreparationRefusedError("rehearsal_initialization_ca_path_invalid")
    with TemporaryDirectory(prefix="quiz-rehearsal-initialize-") as temporary:
        directory = Path(temporary)
        for path, body in (
            (directory / ".env", _migration_environment(settings, secrets)),
            (
                directory / "pg_service.conf",
                (
                    "[rehearsal-initialization]\n"
                    f"hostaddr={host.selected_address}\nsslrootcert={postgres_ca_file}\n"
                    "connect_timeout=5\noptions=-c statement_timeout=30000 -c lock_timeout=5000\n"
                ),
            ),
        ):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(body)
        # service設定はdotenvのcontainer用CAより優先され、URLのhostname/verify-fullは維持する。
        subprocess.run(  # noqa: S603  # 人が呼ぶ固定migration argvのみ。生出力は外へ返さない。
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(repository_root / "backend/alembic.ini"),
                "upgrade",
                "head",
            ],
            cwd=directory,
            env={
                "PGSERVICE": "rehearsal-initialization",
                "PGSERVICEFILE": str(directory / "pg_service.conf"),
            },
            capture_output=True,
            check=True,
            timeout=120,
        )


def _create_role_and_policies(session: Session, secrets: RehearsalInitializationSecrets) -> None:
    _require_current_schema(session)
    for table in SQLModel.metadata.sorted_tables:
        exists = session.scalar(select(select(table.columns[0]).exists()))
        if _Presence.model_validate({"exists": exists}).exists:
            raise RehearsalPreparationRefusedError("rehearsal_initialization_application_not_empty")
    app_url = make_url(secrets.app_database_url.get_secret_value())
    role = sql.Identifier(APPLICATION_ROLE)
    database = sql.Identifier(app_url.database or "")
    # PostgreSQL utility文の識別子/passwordはpsycopgのSQL合成でquoteする。
    statements = [
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}"
        ).format(role, sql.Literal(app_url.password)),
        sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
        sql.SQL("GRANT SELECT ON TABLE public.alembic_version TO {}").format(role),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role),
    ]
    for table in SQLModel.metadata.sorted_tables:
        # 固定リストは人手の準備だけで書き込み、API roleから再配置させない。
        grant = (
            sql.SQL("GRANT SELECT ON TABLE public.{} TO {}")
            if table.name in IMMUTABLE_PARTICIPANT_ID_TABLES
            else sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{} TO {}")
        )
        statements.append(grant.format(sql.Identifier(table.name), role))
    for statement in statements:
        session.connection().exec_driver_sql(
            statement.as_string(), execution_options={"no_parameters": True}
        )
    session.add(configured_scoring_policy(get_quiz_settings()))
    session.add(
        DisplayNamePolicy(
            version=DEMO_DISPLAY_NAME_POLICY_VERSION,
            notice_version="demo-notice-v1",
            max_code_points=20,
            reject_patterns_json="[]",
        )
    )
    session.add(
        AdminAccount(
            username=secrets.operator_username,
            password_hash=create_operator_password_hasher().hash(
                secrets.operator_password.get_secret_value()
            ),
            role=AdminRole.OPERATOR,
            password_policy_version=DEMO_CREDENTIAL_POLICY_VERSION,
            is_demo=True,
            is_revoked=False,
        )
    )
    session.flush()


def initialize_rehearsal_database(
    settings: Settings,
    event: RehearsalEventInput,
    secrets: RehearsalInitializationSecrets,
    *,
    repository_root: Path,
    postgres_ca_file: Path,
) -> None:
    """新規専用DBのみ初期化する。失敗時は停止し、再実行・修復・削除を行わない。"""
    validate_rehearsal_settings(settings, event)
    if not postgres_ca_file.is_absolute():
        raise RehearsalPreparationRefusedError("rehearsal_initialization_ca_path_invalid")
    if (
        settings.database_url != secrets.app_database_url.get_secret_value()
        or event.scoring_policy_version != get_quiz_settings().scoring.version
        or event.display_name_policy_version != DEMO_DISPLAY_NAME_POLICY_VERSION
    ):
        raise RehearsalPreparationRefusedError("rehearsal_initialization_input_mismatch")
    engine: Engine | None = None
    initialized = False
    try:
        admin_url = secrets.admin_database_url.get_secret_value()
        host = validate_remote_dependency_host("database", admin_url)

        def host_engine(
            url: str, *, pool_pre_ping: bool, pool_recycle: int, connect_args: PostgresConnectArgs
        ) -> Engine:
            return create_engine(
                url,
                pool_pre_ping=pool_pre_ping,
                pool_recycle=pool_recycle,
                connect_args={
                    **connect_args,
                    "sslrootcert": str(postgres_ca_file),
                    "options": "-c statement_timeout=30000 -c lock_timeout=5000",
                },
            )

        engine = create_pinned_postgres_engine(admin_url, host, engine_factory=host_engine)
        with Session(engine) as session, session.begin():
            _require_admin_identity(session, secrets)
            _require_fresh_database(session)
        logfire.info("rehearsal human initialization migration started")
        run_existing_migrations(repository_root, settings, secrets, postgres_ca_file, host)
        with Session(engine) as session, session.begin():
            _require_admin_identity(session, secrets)
            _create_role_and_policies(session, secrets)
        initialized = True
    except (
        SQLAlchemyError,
        ValidationError,
        OSError,
        subprocess.SubprocessError,
        DependencyHostValidationError,
    ):
        try:
            raise RehearsalPreparationRefusedError(
                "rehearsal_initialization_failed_inspect_database"
            ) from None
        except RehearsalPreparationRefusedError:
            logfire.exception(
                "rehearsal human initialization failed; inspect database before retry"
            )
            raise
    finally:
        if engine is not None:
            _dispose_engine(engine, event_committed=initialized)
    logfire.info("rehearsal human initialization completed", policies=2, operators=1)
