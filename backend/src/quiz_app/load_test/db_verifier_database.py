from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from socket import getaddrinfo
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

import logfire
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import TextClause

from quiz_app.aws_demo_paths import (
    POSTGRES_CA_FILE_POLICY,
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    ValidatedExternalPath,
    validate_external_regular_file,
)
from quiz_app.config import is_valid_aws_demo_database_url
from quiz_app.dependency_hosts import (
    AddressResolver,
    DependencyHostValidationError,
    validate_remote_dependency_host,
)
from quiz_app.load_test.db_verifier_contracts import (
    DatabaseColumnPrivilegeRow,
    DatabasePrivilegeSummaryRow,
    DatabaseRoleMembershipRow,
    DatabaseRoleRow,
    DatabaseSchemaRevisionRow,
    DatabaseSessionRow,
)

if TYPE_CHECKING:
    from quiz_app.load_test.db_verifier_queries import DbQuerySpec

MAX_DATABASE_URL_BYTES = 8 * 1024
MAX_CA_BUNDLE_BYTES = 1024 * 1024
POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
POSTGRES_STATEMENT_TIMEOUT = "10s"
POSTGRES_LOCK_TIMEOUT = "1s"
POSTGRES_IDLE_TRANSACTION_TIMEOUT = "15s"
POSTGRES_CONNECTION_OPTIONS = (
    "-c default_transaction_read_only=on "
    "-c statement_timeout=10000 "
    "-c lock_timeout=1000 "
    "-c idle_in_transaction_session_timeout=15000 "
    "-c search_path=pg_catalog,public"
)
ALLOWED_ADDITIONAL_ROLES = ("pg_read_all_stats",)

DbVerifierDatabaseErrorCode = Literal[
    "database_url_file_invalid",
    "database_ca_file_invalid",
    "database_ca_content_invalid",
    "database_ca_checksum_invalid",
    "database_url_invalid",
    "database_host_invalid",
    "database_engine_initialization_failed",
    "database_connection_failed",
    "database_session_invalid",
    "database_schema_revision_invalid",
    "database_role_invalid",
    "database_role_membership_invalid",
    "database_privilege_invalid",
    "database_column_privilege_invalid",
    "database_query_contract_invalid",
    "database_query_failed",
    "database_dispose_failed",
]
QueryRow = TypeVar("QueryRow", bound=BaseModel)


class DbVerifierDatabaseError(RuntimeError):
    """credentialやDB例外本文を保持せず固定codeだけを公開する。"""

    def __init__(self, code: DbVerifierDatabaseErrorCode) -> None:
        super().__init__(code)
        self.code = code


class EngineFactory(Protocol):
    def __call__(self, database_url: str, **options: object) -> Engine: ...


@dataclass(frozen=True, slots=True)
class VerifierDatabaseSettings:
    """秘密URLをmaskしたまま、DNS pin済み接続値だけをprocess内で保持する。"""

    database_url: SecretStr
    hostname: str
    hostaddr: str
    role_name: str
    database_name: str
    ca_file: Path
    ca_sha256: str


SESSION_SECURITY_STATEMENT = text(
    """
    SELECT
        current_user::text AS current_role,
        session_user::text AS session_role,
        pg_catalog.current_database()::text AS database_name,
        (pg_catalog.current_setting('transaction_read_only') = 'on') AS is_read_only,
        pg_catalog.current_setting('transaction_isolation') AS isolation_level,
        pg_catalog.current_setting('statement_timeout') AS statement_timeout,
        pg_catalog.current_setting('lock_timeout') AS lock_timeout,
        pg_catalog.current_setting('idle_in_transaction_session_timeout')
            AS idle_transaction_timeout,
        (
            pg_catalog.current_schemas(false) = ARRAY['pg_catalog', 'public']::name[]
        ) AS is_search_path_pinned
    """
)

ROLE_SECURITY_STATEMENT = text(
    """
    SELECT
        role.rolname::text AS role_name,
        role.rolsuper AS is_superuser,
        role.rolcreatedb AS can_create_database,
        role.rolcreaterole AS can_create_role,
        role.rolcanlogin AS can_login,
        role.rolreplication AS is_replication_role,
        role.rolbypassrls AS bypasses_row_level_security,
        role.rolinherit AS inherits_privileges,
        COALESCE(cardinality(role.rolconfig), 0)::bigint AS role_setting_count
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_user
    """
)

ROLE_MEMBERSHIP_STATEMENT = text(
    """
    WITH RECURSIVE inherited_roles(role_oid) AS (
        SELECT membership.roleid
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE member_role.rolname = current_user
        UNION
        SELECT membership.roleid
        FROM pg_catalog.pg_auth_members AS membership
        JOIN inherited_roles
          ON inherited_roles.role_oid = membership.member
    )
    SELECT role.rolname::text AS role_name
    FROM inherited_roles
    JOIN pg_catalog.pg_roles AS role
      ON role.oid = inherited_roles.role_oid
    ORDER BY role.rolname
    """
)

PRIVILEGE_SECURITY_STATEMENT = text(
    """
    WITH current_role_row AS (
        SELECT role.oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
    ),
    application_namespaces AS (
        SELECT namespace.oid
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
    )
    SELECT
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_database AS database
            WHERE database.datdba = (SELECT oid FROM current_role_row)
        ) AS owned_database_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.oid IN (SELECT oid FROM application_namespaces)
              AND namespace.nspowner = (SELECT oid FROM current_role_row)
        ) AS owned_schema_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_class AS relation
            WHERE relation.relnamespace IN (SELECT oid FROM application_namespaces)
              AND relation.relowner = (SELECT oid FROM current_role_row)
        ) AS owned_relation_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_proc AS routine
            WHERE routine.pronamespace IN (SELECT oid FROM application_namespaces)
              AND routine.proowner = (SELECT oid FROM current_role_row)
        ) AS owned_routine_count,
        has_database_privilege(current_user, current_database(), 'CREATE')
            AS has_database_create,
        has_database_privilege(current_user, current_database(), 'TEMPORARY')
            AS has_database_temporary,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.oid IN (SELECT oid FROM application_namespaces)
              AND has_schema_privilege(current_user, namespace.oid, 'CREATE')
        ) AS creatable_schema_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_class AS relation
            WHERE relation.relnamespace IN (SELECT oid FROM application_namespaces)
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND (
                  has_table_privilege(current_user, relation.oid, 'INSERT')
                  OR has_table_privilege(current_user, relation.oid, 'UPDATE')
                  OR has_table_privilege(current_user, relation.oid, 'DELETE')
                  OR has_table_privilege(current_user, relation.oid, 'TRUNCATE')
                  OR has_table_privilege(current_user, relation.oid, 'REFERENCES')
                  OR has_table_privilege(current_user, relation.oid, 'TRIGGER')
              )
        ) AS writable_relation_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_class AS sequence
            WHERE sequence.relnamespace IN (SELECT oid FROM application_namespaces)
              AND sequence.relkind = 'S'
              AND (
                  has_sequence_privilege(current_user, sequence.oid, 'USAGE')
                  OR has_sequence_privilege(current_user, sequence.oid, 'SELECT')
                  OR has_sequence_privilege(current_user, sequence.oid, 'UPDATE')
              )
        ) AS privileged_sequence_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_foreign_data_wrapper AS wrapper
            WHERE has_foreign_data_wrapper_privilege(current_user, wrapper.oid, 'USAGE')
        ) AS usable_foreign_data_wrapper_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_foreign_server AS server
            WHERE has_server_privilege(current_user, server.oid, 'USAGE')
        ) AS usable_foreign_server_count,
        (
            SELECT count(*)::bigint
            FROM pg_catalog.pg_proc AS routine
            WHERE routine.prosecdef
              AND has_function_privilege(current_user, routine.oid, 'EXECUTE')
        ) AS executable_security_definer_count
    """
)

COLUMN_PRIVILEGE_STATEMENT = text(
    """
    SELECT
        namespace.nspname::text AS schema_name,
        relation.relname::text AS table_name,
        attribute.attname::text AS column_name,
        has_column_privilege(
            current_user,
            relation.oid,
            attribute.attnum,
            'SELECT'
        ) AS has_select
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
    WHERE namespace.nspname !~ '^pg_'
      AND namespace.nspname <> 'information_schema'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND has_column_privilege(
          current_user,
          relation.oid,
          attribute.attnum,
          'SELECT'
      )
    ORDER BY relation.relname, attribute.attnum
    """
)

SCHEMA_REVISION_STATEMENT = text(
    """
    SELECT version_num::text AS version_num
    FROM public.alembic_version
    """
)


def _read_validated_file(
    path: Path,
    validated: ValidatedExternalPath,
    *,
    maximum_bytes: int,
    error_code: DbVerifierDatabaseErrorCode,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino)
            != (
                validated.status.st_dev,
                validated.status.st_ino,
            )
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or opened.st_size < 1
            or opened.st_size > maximum_bytes
        ):
            raise DbVerifierDatabaseError(error_code)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        final_status = os.fstat(descriptor)
        if (
            len(body) != opened.st_size
            or len(body) > maximum_bytes
            or (
                final_status.st_dev,
                final_status.st_ino,
                final_status.st_size,
                final_status.st_mtime_ns,
                final_status.st_ctime_ns,
                stat.S_IMODE(final_status.st_mode),
                final_status.st_nlink,
                final_status.st_uid,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                0o600,
                1,
                os.geteuid(),
            )
        ):
            raise DbVerifierDatabaseError(error_code)
        return body
    except DbVerifierDatabaseError:
        raise
    except OSError:
        raise DbVerifierDatabaseError(error_code) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_external_file(
    path: Path,
    repository_root: Path,
    *,
    is_ca: bool,
) -> tuple[bytes, Path]:
    error_code: DbVerifierDatabaseErrorCode = (
        "database_ca_file_invalid" if is_ca else "database_url_file_invalid"
    )
    policy = POSTGRES_CA_FILE_POLICY if is_ca else RUNTIME_ENVIRONMENT_FILE_POLICY
    try:
        validated = validate_external_regular_file(path, repository_root, policy)
    except AwsDemoDeploymentValidationError:
        raise DbVerifierDatabaseError(error_code) from None
    if stat.S_IMODE(validated.status.st_mode) != 0o600:
        raise DbVerifierDatabaseError(error_code)
    maximum_bytes = MAX_CA_BUNDLE_BYTES if is_ca else MAX_DATABASE_URL_BYTES
    return (
        _read_validated_file(
            path,
            validated,
            maximum_bytes=maximum_bytes,
            error_code=error_code,
        ),
        validated.resolved,
    )


def load_verifier_database_settings(
    database_url_file: Path,
    ca_file: Path,
    ca_sha256: str,
    repository_root: Path,
    *,
    resolver: AddressResolver = getaddrinfo,
) -> VerifierDatabaseSettings:
    """外部credential/CAとDNS解決先を検証し、秘密非公開の接続設定へ変換する。"""

    url_body, resolved_url_file = _load_external_file(
        database_url_file,
        repository_root,
        is_ca=False,
    )
    ca_body, resolved_ca_file = _load_external_file(
        ca_file,
        repository_root,
        is_ca=True,
    )
    if resolved_url_file == resolved_ca_file:
        raise DbVerifierDatabaseError("database_ca_file_invalid")

    if not ca_body.startswith(b"-----BEGIN CERTIFICATE-----"):
        raise DbVerifierDatabaseError("database_ca_content_invalid")
    if b"-----END CERTIFICATE-----" not in ca_body or b"PRIVATE KEY" in ca_body:
        raise DbVerifierDatabaseError("database_ca_content_invalid")
    actual_ca_sha256 = hashlib.sha256(ca_body).hexdigest()
    if (
        len(ca_sha256) != 64
        or any(character not in "0123456789abcdef" for character in ca_sha256)
        or not hmac.compare_digest(actual_ca_sha256, ca_sha256)
    ):
        raise DbVerifierDatabaseError("database_ca_checksum_invalid")

    if url_body.endswith(b"\n"):
        url_body = url_body[:-1]
    if not url_body or b"\n" in url_body or b"\r" in url_body:
        raise DbVerifierDatabaseError("database_url_invalid")
    try:
        database_url = url_body.decode("ascii")
    except UnicodeDecodeError:
        raise DbVerifierDatabaseError("database_url_invalid") from None
    if database_url != database_url.strip() or not is_valid_aws_demo_database_url(database_url):
        raise DbVerifierDatabaseError("database_url_invalid")

    try:
        validated_host = validate_remote_dependency_host(
            "database",
            database_url,
            resolver=resolver,
        )
        parsed_url = urlsplit(database_url)
        parsed_hostname = unquote(parsed_url.hostname or "", errors="strict")
        role_name = unquote(parsed_url.username or "", errors="strict")
        database_name = unquote(parsed_url.path.removeprefix("/"), errors="strict")
    except (DependencyHostValidationError, UnicodeError, ValueError):
        raise DbVerifierDatabaseError("database_host_invalid") from None
    if not parsed_hostname or parsed_hostname != validated_host.hostname:
        raise DbVerifierDatabaseError("database_host_invalid")
    if (
        not role_name
        or not database_name
        or "/" in database_name
        or len(role_name.encode("utf-8")) > 63
        or len(database_name.encode("utf-8")) > 63
    ):
        raise DbVerifierDatabaseError("database_url_invalid")
    return VerifierDatabaseSettings(
        database_url=SecretStr(database_url),
        hostname=validated_host.hostname,
        hostaddr=validated_host.selected_address,
        role_name=role_name,
        database_name=database_name,
        ca_file=resolved_ca_file,
        ca_sha256=actual_ca_sha256,
    )


def create_verifier_engine(
    settings: VerifierDatabaseSettings,
    *,
    engine_factory: EngineFactory = create_engine,
) -> Engine:
    """poolを共有せず、TLS/DNS pinと有限timeoutを強制したEngineを作る。"""

    connect_args: dict[str, object] = {
        "connect_timeout": POSTGRES_CONNECT_TIMEOUT_SECONDS,
        "host": settings.hostname,
        "hostaddr": settings.hostaddr,
        "sslmode": "verify-full",
        "sslrootcert": str(settings.ca_file),
        "options": POSTGRES_CONNECTION_OPTIONS,
        "application_name": "quiz-load-test-db-verifier",
    }
    try:
        return engine_factory(
            settings.database_url.get_secret_value(),
            poolclass=NullPool,
            isolation_level="REPEATABLE READ",
            connect_args=connect_args,
            hide_parameters=True,
        )
    except (SQLAlchemyError, TypeError, ValueError):
        raise DbVerifierDatabaseError("database_engine_initialization_failed") from None


def _strict_rows(
    connection: Connection,
    statement: TextClause,
    row_model: type[QueryRow],
    error_code: DbVerifierDatabaseErrorCode,
) -> tuple[QueryRow, ...]:
    try:
        rows = connection.execute(statement).mappings().all()
        return tuple(row_model.model_validate(dict(row)) for row in rows)
    except (SQLAlchemyError, ValidationError, TypeError, ValueError):
        raise DbVerifierDatabaseError(error_code) from None


def validate_database_security(
    connection: Connection,
    *,
    expected_role_name: str,
    expected_database_name: str,
    expected_schema_revision: str,
    allowed_select_columns: frozenset[tuple[str, str]] | None = None,
) -> None:
    """transaction属性・role・全有効権限をdomain SELECTより先にfail-closed検証する。"""

    session_rows = _strict_rows(
        connection,
        SESSION_SECURITY_STATEMENT,
        DatabaseSessionRow,
        "database_session_invalid",
    )
    if len(session_rows) != 1:
        raise DbVerifierDatabaseError("database_session_invalid")
    session = session_rows[0]
    if (
        session.current_role != session.session_role
        or session.current_role != expected_role_name
        or session.database_name != expected_database_name
    ):
        raise DbVerifierDatabaseError("database_session_invalid")

    role_rows = _strict_rows(
        connection,
        ROLE_SECURITY_STATEMENT,
        DatabaseRoleRow,
        "database_role_invalid",
    )
    if len(role_rows) != 1 or role_rows[0].role_name != session.current_role:
        raise DbVerifierDatabaseError("database_role_invalid")

    membership_rows = _strict_rows(
        connection,
        ROLE_MEMBERSHIP_STATEMENT,
        DatabaseRoleMembershipRow,
        "database_role_membership_invalid",
    )
    memberships = tuple(row.role_name for row in membership_rows)
    if memberships != ALLOWED_ADDITIONAL_ROLES:
        raise DbVerifierDatabaseError("database_role_membership_invalid")

    privilege_rows = _strict_rows(
        connection,
        PRIVILEGE_SECURITY_STATEMENT,
        DatabasePrivilegeSummaryRow,
        "database_privilege_invalid",
    )
    if len(privilege_rows) != 1:
        raise DbVerifierDatabaseError("database_privilege_invalid")

    if allowed_select_columns is None:
        from quiz_app.load_test.db_verifier_queries import APPLICATION_SELECT_COLUMN_ALLOWLIST

        allowed_select_columns = APPLICATION_SELECT_COLUMN_ALLOWLIST
    column_rows = _strict_rows(
        connection,
        COLUMN_PRIVILEGE_STATEMENT,
        DatabaseColumnPrivilegeRow,
        "database_column_privilege_invalid",
    )
    selected_columns = frozenset(
        (row.table_name, row.column_name) for row in column_rows if row.has_select
    )
    if selected_columns != allowed_select_columns:
        raise DbVerifierDatabaseError("database_column_privilege_invalid")

    schema_rows = _strict_rows(
        connection,
        SCHEMA_REVISION_STATEMENT,
        DatabaseSchemaRevisionRow,
        "database_schema_revision_invalid",
    )
    if len(schema_rows) != 1 or schema_rows[0].version_num != expected_schema_revision:
        raise DbVerifierDatabaseError("database_schema_revision_invalid")


@dataclass(slots=True)
class ReadOnlyDatabaseSnapshot:
    """検証済みread-only transaction内で固定query specだけを実行する。"""

    _connection: Connection

    def execute_query(
        self,
        spec: DbQuerySpec[QueryRow],
        parameters: Mapping[str, object],
    ) -> tuple[QueryRow, ...]:
        from quiz_app.load_test.db_verifier_queries import (
            DbQueryValidationError,
            query_spec_by_id,
            validate_query_parameters,
        )

        try:
            registered_spec = query_spec_by_id(spec.query_id)
            if registered_spec is not spec:
                raise DbQueryValidationError("db_verifier_query_not_registered")
            validated_parameters = validate_query_parameters(spec, parameters)
        except DbQueryValidationError:
            raise DbVerifierDatabaseError("database_query_contract_invalid") from None
        try:
            rows = (
                self._connection.execute(spec.statement, validated_parameters)
                .mappings()
                .fetchmany(spec.max_rows + 1)
            )
            from quiz_app.load_test.db_verifier_queries import (
                DbQueryValidationError,
                validate_query_rows,
            )

            return validate_query_rows(spec, tuple(dict(row) for row in rows))
        except DbVerifierDatabaseError:
            raise
        except (DbQueryValidationError, SQLAlchemyError, ValidationError, TypeError, ValueError):
            raise DbVerifierDatabaseError("database_query_failed") from None


class VerifierDatabase:
    """Engine所有権とsnapshot lifetimeを明示するDB verifier境界。"""

    def __init__(self, engine: Engine, settings: VerifierDatabaseSettings) -> None:
        self._engine = engine
        self._settings = settings

    @contextmanager
    def read_only_snapshot(
        self,
        *,
        expected_schema_revision: str,
    ) -> Iterator[ReadOnlyDatabaseSnapshot]:
        """REPEATABLE READ/READ ONLYを実測確認後にsnapshotを公開する。"""

        try:
            with self._engine.connect() as base_connection:
                connection = base_connection.execution_options(postgresql_readonly=True)
                with connection.begin():
                    validate_database_security(
                        connection,
                        expected_role_name=self._settings.role_name,
                        expected_database_name=self._settings.database_name,
                        expected_schema_revision=expected_schema_revision,
                    )
                    yield ReadOnlyDatabaseSnapshot(connection)
        except DbVerifierDatabaseError:
            raise
        except SQLAlchemyError:
            logfire.error(
                "load-test DB verifier connection failed",
                error_code="database_connection_failed",
            )
            raise DbVerifierDatabaseError("database_connection_failed") from None

    def dispose(self) -> None:
        """接続資源を解放し、driver例外本文は公開しない。"""

        try:
            self._engine.dispose()
        except (SQLAlchemyError, OSError):
            logfire.error(
                "load-test DB verifier dispose failed",
                error_code="database_dispose_failed",
            )
            raise DbVerifierDatabaseError("database_dispose_failed") from None
