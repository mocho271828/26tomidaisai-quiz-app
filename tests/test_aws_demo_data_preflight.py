import secrets
from uuid import UUID

import pytest
from sqlalchemy import Connection, create_engine, event, text

from quiz_app import deployment_preflight
from quiz_app.config import Settings
from quiz_app.deployment_preflight import (
    DeploymentDataReadiness,
    RuntimeDependencies,
    inspect_deployment_data,
)
from quiz_app.operator.auth_service import create_operator_password_hasher

AWS_DEMO_EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def prepared_participant_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    # PostgreSQL catalog専用検証は別の回帰で扱い、このSQLite fixtureは既存開催条件に絞る。
    def inspect_ids(connection: Connection, event_id: UUID) -> bool:
        assert connection.dialect.name == "sqlite"
        return event_id == AWS_DEMO_EVENT_ID

    monkeypatch.setattr(deployment_preflight, "inspect_participant_id_readiness", inspect_ids)


class PreflightRedisClient:
    """data query試験で接続資源の解放契約だけを満たす。"""

    def publish(self, channel: str, message: str) -> int:
        del channel, message
        return 1

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


def _runtime_dependencies() -> RuntimeDependencies:
    return RuntimeDependencies(
        engine=create_engine("sqlite+pysqlite:///:memory:"),
        redis_client=PreflightRedisClient(),
    )


def _prepare_deployment_data(
    dependencies: RuntimeDependencies,
    *,
    password_hash: str | None = None,
    operator_username: str = "aws-demo-operator",
    round_count: int = 1,
    include_additional_event: bool = False,
) -> Settings:
    """preflightのread-only判定専用に最小tableとdataを用意する。"""

    current_password_hash = password_hash or create_operator_password_hasher().hash(
        secrets.token_urlsafe(24)
    )
    with dependencies.engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id TEXT, slug TEXT)"))
        connection.execute(text("CREATE TABLE quiz_rounds (event_id TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE admin_accounts ("
                "username TEXT, password_hash TEXT, role TEXT, "
                "is_demo BOOLEAN, is_revoked BOOLEAN)"
            )
        )
        connection.execute(
            text("INSERT INTO events (id, slug) VALUES (:id, :slug)"),
            {"id": str(AWS_DEMO_EVENT_ID), "slug": "aws-demo-2026"},
        )
        if include_additional_event:
            connection.execute(
                text("INSERT INTO events (id, slug) VALUES (:id, :slug)"),
                {
                    "id": "71000000-0000-4000-8000-000000000001",
                    "slug": "additional-event",
                },
            )
        for _ in range(round_count):
            connection.execute(
                text("INSERT INTO quiz_rounds (event_id) VALUES (:event_id)"),
                {"event_id": str(AWS_DEMO_EVENT_ID)},
            )
        connection.execute(
            text(
                "INSERT INTO admin_accounts "
                "(username, password_hash, role, is_demo, is_revoked) "
                "VALUES (:username, :password_hash, 'Operator', 1, 0)"
            ),
            {"username": operator_username, "password_hash": current_password_hash},
        )
    return Settings.model_construct(
        aws_demo_expected_event_id=AWS_DEMO_EVENT_ID,
        aws_demo_round_scope="single-round",
    )


def test_deployment_data_inspection_requires_exact_aws_demo_records() -> None:
    dependencies = _runtime_dependencies()
    settings = _prepare_deployment_data(dependencies)
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

    readiness = inspect_deployment_data(dependencies, settings)

    assert readiness == DeploymentDataReadiness(
        status="ready",
        event="ok",
        round_scope="ok",
        operator="ok",
        participant_ids="not_checked",
    )
    assert database_statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in database_statements)
    dependencies.close()


def test_deployment_data_inspection_rejects_additional_event_and_multiple_rounds() -> None:
    dependencies = _runtime_dependencies()
    settings = _prepare_deployment_data(
        dependencies,
        operator_username="demo-operator",
        round_count=2,
        include_additional_event=True,
    )

    readiness = inspect_deployment_data(dependencies, settings)

    assert readiness == DeploymentDataReadiness(
        status="not_ready",
        event="invalid",
        round_scope="invalid",
        operator="invalid",
        participant_ids="not_checked",
    )
    dependencies.close()


def test_deployment_data_inspection_rejects_unexpected_only_event_id() -> None:
    dependencies = _runtime_dependencies()
    settings = _prepare_deployment_data(dependencies).model_copy(
        update={"aws_demo_expected_event_id": UUID("72000000-0000-4000-8000-000000000001")}
    )

    readiness = inspect_deployment_data(dependencies, settings)

    assert readiness.event == "invalid"
    assert readiness.status == "not_ready"
    dependencies.close()


def test_deployment_data_inspection_rejects_malformed_argon2id_hash() -> None:
    dependencies = _runtime_dependencies()
    hash_type = "argon2id"
    malformed_encoded_value = f"${hash_type}$test-hash"
    settings = _prepare_deployment_data(
        dependencies,
        password_hash=malformed_encoded_value,
    )

    readiness = inspect_deployment_data(dependencies, settings)

    assert readiness == DeploymentDataReadiness(
        status="not_ready",
        event="ok",
        round_scope="ok",
        operator="invalid",
        participant_ids="not_checked",
    )
    dependencies.close()


@pytest.mark.parametrize(
    ("round_count", "question_count", "expected"),
    [
        (0, 6, "invalid"),
        (1, 6, "ok"),
        (6, 6, "ok"),
        (7, 6, "invalid"),
        (1, 5, "ok"),
        (1, 8, "ok"),
        (8, 8, "ok"),
        (9, 8, "invalid"),
        (1, 3, "ok"),
        (3, 3, "ok"),
        (1, 2, "invalid"),
    ],
)
def test_rehearsal_rounds_are_bounded_by_variable_registered_question_count(
    round_count: int,
    question_count: int,
    expected: str,
) -> None:
    dependencies = _runtime_dependencies()
    settings = _prepare_deployment_data(dependencies, round_count=round_count).model_copy(
        update={"aws_demo_round_scope": "rehearsal-six-round"},
    )
    with dependencies.engine.begin() as connection:
        connection.execute(text("CREATE TABLE questions (event_id TEXT)"))
        for _ in range(question_count):
            connection.execute(
                text("INSERT INTO questions (event_id) VALUES (:event_id)"),
                {"event_id": str(AWS_DEMO_EVENT_ID)},
            )
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(dependencies.engine, "before_cursor_execute", record_statement)
    readiness = inspect_deployment_data(dependencies, settings)

    assert readiness.round_scope == expected
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    dependencies.close()


@pytest.mark.parametrize("scope", ["single-round", "load-test-ten-round"])
def test_other_deployment_scopes_keep_the_existing_data_checks(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    dependencies = _runtime_dependencies()
    settings = _prepare_deployment_data(dependencies).model_copy(
        update={"aws_demo_round_scope": scope}
    )

    def reject_pool_check(*_: object) -> bool:
        pytest.fail("participant ID preparation check is limited to rehearsal deployments")

    monkeypatch.setattr(deployment_preflight, "inspect_participant_id_readiness", reject_pool_check)
    readiness = inspect_deployment_data(dependencies, settings)

    assert readiness.status == "ready"
    assert readiness.participant_ids == "not_checked"
    dependencies.close()
