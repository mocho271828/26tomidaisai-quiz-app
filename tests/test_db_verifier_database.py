import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from socket import AF_INET, SOCK_STREAM
from typing import Self, cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import TextClause

from quiz_app.load_test import db_verifier_database as database_module
from quiz_app.load_test.db_verifier_contracts import (
    DB_VERIFIER_REQUEST_ADAPTER,
    FinalVerificationInput,
    LockObservationInput,
    PerRoundVerificationInput,
)
from quiz_app.load_test.db_verifier_database import (
    ALLOWED_ADDITIONAL_ROLES,
    COLUMN_PRIVILEGE_STATEMENT,
    POSTGRES_CONNECTION_OPTIONS,
    PRIVILEGE_SECURITY_STATEMENT,
    ROLE_MEMBERSHIP_STATEMENT,
    ROLE_SECURITY_STATEMENT,
    SCHEMA_REVISION_STATEMENT,
    SESSION_SECURITY_STATEMENT,
    DbVerifierDatabaseError,
    VerifierDatabase,
    create_verifier_engine,
    load_verifier_database_settings,
    validate_database_security,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CA_BODY = b"-----BEGIN CERTIFICATE-----\nY2EtYnVuZGxl\n-----END CERTIFICATE-----\n"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _database_url() -> str:
    credential = "uN9xQ2vR7mK4pL8s"
    return (
        "postgresql+psycopg://load_verifier:"
        f"{credential}@db.aws.internal:5432/quiz_load_test?sslmode=verify-full"
    )


def _resolver(hostname: str, _port: object, *, type: int) -> list[tuple[object, ...]]:
    assert hostname == "db.aws.internal"
    assert type == SOCK_STREAM
    return [
        (AF_INET, SOCK_STREAM, 6, "", ("10.0.0.9", 5432)),
        (AF_INET, SOCK_STREAM, 6, "", ("10.0.0.8", 5432)),
    ]


def _write_secure(path: Path, body: bytes) -> Path:
    path.write_bytes(body)
    path.chmod(0o600)
    return path


def _settings(tmp_path: Path) -> database_module.VerifierDatabaseSettings:
    url_file = _write_secure(
        tmp_path / "database-url", (_database_url() + "\n").encode()
    )
    ca_file = _write_secure(tmp_path / "postgres-ca.pem", CA_BODY)
    return load_verifier_database_settings(
        url_file,
        ca_file,
        hashlib.sha256(CA_BODY).hexdigest(),
        REPOSITORY_ROOT,
        resolver=_resolver,
    )


def _round_request(round_number: int = 1) -> dict[str, object]:
    return {
        "format": "load-test-round-verification-request-v2",
        "verification_id": UUID("10000000-0000-4000-8000-000000000001"),
        "run_id": UUID("10000000-0000-4000-8000-000000000002"),
        "event_id": UUID("10000000-0000-4000-8000-000000000003"),
        "source_commit": "a" * 40,
        "schema_revision": "20260804_0001",
        "manifest_sha256": "b" * 64,
        "scenario_sha256": "c" * 64,
        "round_number": round_number,
        "round_id": UUID("10000000-0000-4000-8000-000000000004"),
        "question_id": UUID("10000000-0000-4000-8000-000000000005"),
        "scenario_condition": "none",
        "verification_requested_head_sha256": "d" * 64,
        "generator_evidence": {
            "name": "generator-round.json",
            "sha256": "e" * 64,
            "size_bytes": 100,
        },
        "oracle": {
            "total_answer_count": 500,
            "virtual_session_count": 600,
            "virtual_answer_count": 500,
            "option_counts": {"a": 126, "b": 125, "c": 125, "d": 124},
            "round_start_width_max_ms": 1000,
            "unresolved_answer_maximum": 0,
            "missed_round_maximum": 0,
            "duplicate_answer_maximum": 0,
            "misleading_success_maximum": 0,
            "replay_attempt_count": 0,
            "replay_same_answer_id_count": 0,
            "replay_row_change_maximum": 0,
            "receipt_discard_count": 0,
            "answer_status_query_count": 0,
            "status_recovered_count": 0,
        },
        "requested_at": NOW,
    }


def test_db_verifier_input_is_strict_discriminated_union() -> None:
    per_round = DB_VERIFIER_REQUEST_ADAPTER.validate_python(
        {
            "format": "load-test-db-verifier-input-v1",
            "mode": "per-round",
            "request": _round_request(),
        }
    )
    final = DB_VERIFIER_REQUEST_ADAPTER.validate_python(
        {
            "format": "load-test-db-verifier-input-v1",
            "mode": "final",
            "request": _round_request(10),
        }
    )
    lock_observation = DB_VERIFIER_REQUEST_ADAPTER.validate_python(
        {
            "format": "load-test-db-lock-observation-input-v1",
            "mode": "observe-locks",
            "run_id": UUID("20000000-0000-4000-8000-000000000001"),
            "event_id": UUID("20000000-0000-4000-8000-000000000002"),
            "source_commit": "a" * 40,
            "schema_revision": "20260804_0001",
            "manifest_sha256": "b" * 64,
            "scenario_sha256": "c" * 64,
            "round_number": 1,
            "observation_start_at": NOW,
            "observation_end_at": NOW + timedelta(seconds=2),
        }
    )

    assert isinstance(per_round, PerRoundVerificationInput)
    assert isinstance(final, FinalVerificationInput)
    assert isinstance(lock_observation, LockObservationInput)


def test_final_input_requires_round_ten() -> None:
    with pytest.raises(ValidationError):
        DB_VERIFIER_REQUEST_ADAPTER.validate_python(
            {
                "format": "load-test-db-verifier-input-v1",
                "mode": "final",
                "request": _round_request(9),
            }
        )


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"unexpected": True},
        {"observation_end_at": NOW},
        {"observation_start_at": NOW.replace(tzinfo=None)},
    ],
)
def test_lock_observation_input_rejects_extra_or_invalid_time(
    invalid_update: dict[str, object],
) -> None:
    value: dict[str, object] = {
        "format": "load-test-db-lock-observation-input-v1",
        "mode": "observe-locks",
        "run_id": UUID("20000000-0000-4000-8000-000000000001"),
        "event_id": UUID("20000000-0000-4000-8000-000000000002"),
        "source_commit": "a" * 40,
        "schema_revision": "20260804_0001",
        "manifest_sha256": "b" * 64,
        "scenario_sha256": "c" * 64,
        "round_number": 1,
        "observation_start_at": NOW,
        "observation_end_at": NOW + timedelta(seconds=2),
    }
    value.update(invalid_update)

    with pytest.raises(ValidationError):
        DB_VERIFIER_REQUEST_ADAPTER.validate_python(value)


def test_load_settings_uses_external_0600_files_and_pins_dns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert settings.database_url.get_secret_value() == _database_url()
    assert settings.hostname == "db.aws.internal"
    assert settings.hostaddr == "10.0.0.8"
    assert settings.role_name == "load_verifier"
    assert settings.database_name == "quiz_load_test"
    assert settings.ca_file == (tmp_path / "postgres-ca.pem").resolve()
    assert settings.ca_sha256 == hashlib.sha256(CA_BODY).hexdigest()
    assert _database_url() not in repr(settings)


@pytest.mark.parametrize("target", ["url", "ca"])
@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_load_settings_rejects_non_exact_file_mode(
    tmp_path: Path,
    target: str,
    mode: int,
) -> None:
    url_file = _write_secure(tmp_path / "database-url", _database_url().encode())
    ca_file = _write_secure(tmp_path / "postgres-ca.pem", CA_BODY)
    (url_file if target == "url" else ca_file).chmod(mode)

    expected_code = (
        "database_url_file_invalid" if target == "url" else "database_ca_file_invalid"
    )
    with pytest.raises(DbVerifierDatabaseError, match=f"^{expected_code}$"):
        load_verifier_database_settings(
            url_file,
            ca_file,
            hashlib.sha256(CA_BODY).hexdigest(),
            REPOSITORY_ROOT,
            resolver=_resolver,
        )


@pytest.mark.parametrize("target", ["url", "ca"])
def test_load_settings_rejects_symlink(tmp_path: Path, target: str) -> None:
    real_url = _write_secure(tmp_path / "real-url", _database_url().encode())
    real_ca = _write_secure(tmp_path / "real-ca", CA_BODY)
    url_file = tmp_path / "database-url"
    ca_file = tmp_path / "postgres-ca.pem"
    if target == "url":
        url_file.symlink_to(real_url)
        ca_file = real_ca
    else:
        url_file = real_url
        ca_file.symlink_to(real_ca)

    expected_code = (
        "database_url_file_invalid" if target == "url" else "database_ca_file_invalid"
    )
    with pytest.raises(DbVerifierDatabaseError, match=f"^{expected_code}$"):
        load_verifier_database_settings(
            url_file,
            ca_file,
            hashlib.sha256(CA_BODY).hexdigest(),
            REPOSITORY_ROOT,
            resolver=_resolver,
        )


def test_load_settings_rejects_hard_linked_database_url(tmp_path: Path) -> None:
    url_file = _write_secure(tmp_path / "database-url", _database_url().encode())
    os.link(url_file, tmp_path / "database-url-alias")
    ca_file = _write_secure(tmp_path / "postgres-ca.pem", CA_BODY)

    with pytest.raises(DbVerifierDatabaseError, match="^database_url_file_invalid$"):
        load_verifier_database_settings(
            url_file,
            ca_file,
            hashlib.sha256(CA_BODY).hexdigest(),
            REPOSITORY_ROOT,
            resolver=_resolver,
        )


@pytest.mark.parametrize(
    ("ca_body", "digest", "code"),
    [
        (
            b"not-a-certificate",
            hashlib.sha256(b"not-a-certificate").hexdigest(),
            "database_ca_content_invalid",
        ),
        (
            CA_BODY + b"-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n",
            hashlib.sha256(
                CA_BODY
                + b"-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
            ).hexdigest(),
            "database_ca_content_invalid",
        ),
        (CA_BODY, "0" * 64, "database_ca_checksum_invalid"),
        (CA_BODY, "INVALID", "database_ca_checksum_invalid"),
    ],
)
def test_load_settings_rejects_invalid_ca(
    tmp_path: Path,
    ca_body: bytes,
    digest: str,
    code: str,
) -> None:
    url_file = _write_secure(tmp_path / "database-url", _database_url().encode())
    ca_file = _write_secure(tmp_path / "postgres-ca.pem", ca_body)

    with pytest.raises(DbVerifierDatabaseError, match=f"^{code}$"):
        load_verifier_database_settings(
            url_file,
            ca_file,
            digest,
            REPOSITORY_ROOT,
            resolver=_resolver,
        )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://user:long-credential@db.aws.internal/db",
        "postgresql+psycopg://user:long-credential@db.aws.internal/db?sslmode=require",
        "postgresql+psycopg://user:long-credential@localhost/db?sslmode=verify-full",
        _database_url() + "\nsecond-line",
        " " + _database_url(),
    ],
)
def test_load_settings_rejects_invalid_url(tmp_path: Path, database_url: str) -> None:
    url_file = _write_secure(tmp_path / "database-url", database_url.encode())
    ca_file = _write_secure(tmp_path / "postgres-ca.pem", CA_BODY)

    with pytest.raises(DbVerifierDatabaseError, match="^database_url_invalid$"):
        load_verifier_database_settings(
            url_file,
            ca_file,
            hashlib.sha256(CA_BODY).hexdigest(),
            REPOSITORY_ROOT,
            resolver=_resolver,
        )


def test_load_settings_rejects_unapproved_dns_result_without_leaking_url(
    tmp_path: Path,
) -> None:
    url_file = _write_secure(tmp_path / "database-url", _database_url().encode())
    ca_file = _write_secure(tmp_path / "postgres-ca.pem", CA_BODY)

    def resolver(
        _hostname: str, _port: object, *, type: int
    ) -> list[tuple[object, ...]]:
        assert type == SOCK_STREAM
        return [(AF_INET, SOCK_STREAM, 6, "", ("127.0.0.1", 5432))]

    with pytest.raises(
        DbVerifierDatabaseError, match="^database_host_invalid$"
    ) as captured:
        load_verifier_database_settings(
            url_file,
            ca_file,
            hashlib.sha256(CA_BODY).hexdigest(),
            REPOSITORY_ROOT,
            resolver=resolver,
        )
    assert _database_url() not in str(captured.value)


def test_create_engine_uses_nullpool_tls_pin_and_finite_timeouts(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    captured: dict[str, object] = {}
    expected_engine = cast(Engine, object())

    def engine_factory(database_url: str, **options: object) -> Engine:
        captured["database_url"] = database_url
        captured.update(options)
        return expected_engine

    assert (
        create_verifier_engine(settings, engine_factory=engine_factory)
        is expected_engine
    )
    assert captured["database_url"] == _database_url()
    assert captured["poolclass"] is NullPool
    assert captured["isolation_level"] == "REPEATABLE READ"
    assert captured["hide_parameters"] is True
    assert captured["connect_args"] == {
        "connect_timeout": 5,
        "host": "db.aws.internal",
        "hostaddr": "10.0.0.8",
        "sslmode": "verify-full",
        "sslrootcert": str((tmp_path / "postgres-ca.pem").resolve()),
        "options": POSTGRES_CONNECTION_OPTIONS,
        "application_name": "quiz-load-test-db-verifier",
    }


class FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeMappings:
        return FakeMappings(self._rows)


class SecurityConnection:
    def __init__(self, rows: Mapping[int, list[dict[str, object]]]) -> None:
        self.rows = rows
        self.statements: list[TextClause] = []

    def execute(self, statement: TextClause) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.rows[id(statement)])


def _valid_security_rows() -> dict[int, list[dict[str, object]]]:
    return {
        id(SESSION_SECURITY_STATEMENT): [
            {
                "current_role": "load_verifier",
                "session_role": "load_verifier",
                "database_name": "quiz_load_test",
                "is_read_only": True,
                "isolation_level": "repeatable read",
                "statement_timeout": "10s",
                "lock_timeout": "1s",
                "idle_transaction_timeout": "15s",
                "is_search_path_pinned": True,
            }
        ],
        id(ROLE_SECURITY_STATEMENT): [
            {
                "role_name": "load_verifier",
                "is_superuser": False,
                "can_create_database": False,
                "can_create_role": False,
                "can_login": True,
                "is_replication_role": False,
                "bypasses_row_level_security": False,
                "inherits_privileges": True,
                "role_setting_count": 0,
            }
        ],
        id(ROLE_MEMBERSHIP_STATEMENT): [{"role_name": ALLOWED_ADDITIONAL_ROLES[0]}],
        id(PRIVILEGE_SECURITY_STATEMENT): [
            {
                "owned_database_count": 0,
                "owned_schema_count": 0,
                "owned_relation_count": 0,
                "owned_routine_count": 0,
                "has_database_create": False,
                "has_database_temporary": False,
                "creatable_schema_count": 0,
                "writable_relation_count": 0,
                "privileged_sequence_count": 0,
                "usable_foreign_data_wrapper_count": 0,
                "usable_foreign_server_count": 0,
                "executable_security_definer_count": 0,
            }
        ],
        id(COLUMN_PRIVILEGE_STATEMENT): [
            {
                "schema_name": "public",
                "table_name": "answers",
                "column_name": "id",
                "has_select": True,
            },
            {
                "schema_name": "public",
                "table_name": "admin_sessions",
                "column_name": "token_hash",
                "has_select": False,
            },
        ],
        id(SCHEMA_REVISION_STATEMENT): [{"version_num": "20260804_0001"}],
    }


def test_security_validation_accepts_only_exact_minimum_role_and_columns() -> None:
    rows = _valid_security_rows()
    connection = SecurityConnection(rows)

    validate_database_security(
        cast(Connection, connection),
        expected_role_name="load_verifier",
        expected_database_name="quiz_load_test",
        expected_schema_revision="20260804_0001",
        allowed_select_columns=frozenset({("answers", "id")}),
    )

    assert connection.statements == [
        SESSION_SECURITY_STATEMENT,
        ROLE_SECURITY_STATEMENT,
        ROLE_MEMBERSHIP_STATEMENT,
        PRIVILEGE_SECURITY_STATEMENT,
        COLUMN_PRIVILEGE_STATEMENT,
        SCHEMA_REVISION_STATEMENT,
    ]


@pytest.mark.parametrize(
    ("statement", "field", "value", "code"),
    [
        (SESSION_SECURITY_STATEMENT, "is_read_only", False, "database_session_invalid"),
        (
            SESSION_SECURITY_STATEMENT,
            "isolation_level",
            "read committed",
            "database_session_invalid",
        ),
        (
            SESSION_SECURITY_STATEMENT,
            "is_search_path_pinned",
            False,
            "database_session_invalid",
        ),
        (
            SESSION_SECURITY_STATEMENT,
            "current_role",
            "different_verifier",
            "database_session_invalid",
        ),
        (
            SESSION_SECURITY_STATEMENT,
            "database_name",
            "different_database",
            "database_session_invalid",
        ),
        (ROLE_SECURITY_STATEMENT, "is_superuser", True, "database_role_invalid"),
        (ROLE_SECURITY_STATEMENT, "can_create_database", True, "database_role_invalid"),
        (ROLE_SECURITY_STATEMENT, "can_create_role", True, "database_role_invalid"),
        (ROLE_SECURITY_STATEMENT, "is_replication_role", True, "database_role_invalid"),
        (
            ROLE_SECURITY_STATEMENT,
            "bypasses_row_level_security",
            True,
            "database_role_invalid",
        ),
        (ROLE_SECURITY_STATEMENT, "role_setting_count", 1, "database_role_invalid"),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "owned_database_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "owned_schema_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "owned_relation_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "owned_routine_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "has_database_create",
            True,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "has_database_temporary",
            True,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "creatable_schema_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "writable_relation_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "privileged_sequence_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "usable_foreign_data_wrapper_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "usable_foreign_server_count",
            1,
            "database_privilege_invalid",
        ),
        (
            PRIVILEGE_SECURITY_STATEMENT,
            "executable_security_definer_count",
            1,
            "database_privilege_invalid",
        ),
    ],
)
def test_security_validation_rejects_dangerous_role_or_privilege(
    statement: TextClause,
    field: str,
    value: object,
    code: str,
) -> None:
    rows = _valid_security_rows()
    rows[id(statement)][0][field] = value

    with pytest.raises(DbVerifierDatabaseError, match=f"^{code}$"):
        validate_database_security(
            cast(Connection, SecurityConnection(rows)),
            expected_role_name="load_verifier",
            expected_database_name="quiz_load_test",
            expected_schema_revision="20260804_0001",
            allowed_select_columns=frozenset({("answers", "id")}),
        )


@pytest.mark.parametrize(
    "memberships",
    [[], [{"role_name": "pg_read_all_stats"}, {"role_name": "application_owner"}]],
)
def test_security_validation_requires_only_pg_read_all_stats(
    memberships: list[dict[str, object]],
) -> None:
    rows = _valid_security_rows()
    rows[id(ROLE_MEMBERSHIP_STATEMENT)] = memberships

    with pytest.raises(
        DbVerifierDatabaseError,
        match="^database_role_membership_invalid$",
    ):
        validate_database_security(
            cast(Connection, SecurityConnection(rows)),
            expected_role_name="load_verifier",
            expected_database_name="quiz_load_test",
            expected_schema_revision="20260804_0001",
            allowed_select_columns=frozenset({("answers", "id")}),
        )


@pytest.mark.parametrize(
    "allowed_columns",
    [frozenset(), frozenset({("answers", "id"), ("answers", "participant_id")})],
)
def test_security_validation_requires_exact_select_column_allowlist(
    allowed_columns: frozenset[tuple[str, str]],
) -> None:
    with pytest.raises(
        DbVerifierDatabaseError,
        match="^database_column_privilege_invalid$",
    ):
        validate_database_security(
            cast(Connection, SecurityConnection(_valid_security_rows())),
            expected_role_name="load_verifier",
            expected_database_name="quiz_load_test",
            expected_schema_revision="20260804_0001",
            allowed_select_columns=allowed_columns,
        )


def test_security_validation_requires_exact_schema_revision() -> None:
    rows = _valid_security_rows()
    rows[id(SCHEMA_REVISION_STATEMENT)] = [{"version_num": "different_revision"}]

    with pytest.raises(
        DbVerifierDatabaseError,
        match="^database_schema_revision_invalid$",
    ):
        validate_database_security(
            cast(Connection, SecurityConnection(rows)),
            expected_role_name="load_verifier",
            expected_database_name="quiz_load_test",
            expected_schema_revision="20260804_0001",
            allowed_select_columns=frozenset({("answers", "id")}),
        )


class SnapshotConnection:
    def __init__(self) -> None:
        self.execution_options_values: dict[str, object] = {}
        self.did_begin = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execution_options(self, **options: object) -> Self:
        self.execution_options_values = options
        return self

    @contextmanager
    def begin(self) -> Iterator[None]:
        self.did_begin = True
        yield


class SnapshotEngine:
    def __init__(self, connection: SnapshotConnection) -> None:
        self.connection = connection
        self.did_dispose = False

    def connect(self) -> SnapshotConnection:
        return self.connection

    def dispose(self) -> None:
        self.did_dispose = True


def test_verifier_database_checks_security_inside_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = SnapshotConnection()
    engine = SnapshotEngine(connection)
    checked: list[tuple[object, dict[str, object]]] = []

    def record_security_check(actual: object, **kwargs: object) -> None:
        checked.append((actual, kwargs))

    monkeypatch.setattr(
        database_module,
        "validate_database_security",
        record_security_check,
    )
    database = VerifierDatabase(cast(Engine, engine), _settings(tmp_path))

    with database.read_only_snapshot(
        expected_schema_revision="20260804_0001"
    ) as snapshot:
        assert snapshot._connection is connection

    assert connection.execution_options_values == {"postgresql_readonly": True}
    assert connection.did_begin is True
    assert checked == [
        (
            connection,
            {
                "expected_role_name": "load_verifier",
                "expected_database_name": "quiz_load_test",
                "expected_schema_revision": "20260804_0001",
            },
        )
    ]
    database.dispose()
    assert engine.did_dispose is True


def test_verifier_database_redacts_sqlalchemy_connection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedEngine:
        def connect(self) -> Connection:
            raise SQLAlchemyError(_database_url())

    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        database_module.logfire,
        "error",
        lambda message, **fields: logged.append((message, fields)),
    )
    database = VerifierDatabase(cast(Engine, FailedEngine()), _settings(tmp_path))
    with (
        pytest.raises(
            DbVerifierDatabaseError, match="^database_connection_failed$"
        ) as captured,
        database.read_only_snapshot(expected_schema_revision="20260804_0001"),
    ):
        pytest.fail("snapshot must not be yielded")
    assert _database_url() not in str(captured.value)
    assert logged == [
        (
            "load-test DB verifier connection failed",
            {"error_code": "database_connection_failed"},
        )
    ]


def test_all_database_statements_are_fixed_selects_without_statement_separator() -> (
    None
):
    for statement in (
        SESSION_SECURITY_STATEMENT,
        ROLE_SECURITY_STATEMENT,
        ROLE_MEMBERSHIP_STATEMENT,
        PRIVILEGE_SECURITY_STATEMENT,
        COLUMN_PRIVILEGE_STATEMENT,
        SCHEMA_REVISION_STATEMENT,
    ):
        normalized = str(statement).strip().upper()
        assert normalized.startswith(
            ("SELECT", "WITH RECURSIVE", "WITH CURRENT_ROLE_ROW")
        )
        assert ";" not in normalized
