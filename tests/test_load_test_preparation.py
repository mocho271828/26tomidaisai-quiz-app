from typing import cast
from uuid import uuid5

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine
from sqlmodel import SQLModel
from test_load_test_bootstrap import FakeEngine, FakeSession, _formal_quiz_settings, _manifest

from quiz_app.load_test import bootstrap, preparation
from quiz_app.load_test.bootstrap import LoadTestBootstrapRefusedError
from quiz_app.models import (
    AdminAccount,
    DisplayNamePolicy,
    Event,
    EventQuizConfiguration,
    Question,
    ScoringPolicy,
)
from quiz_app.operator.auth_service import create_operator_password_hasher
from quiz_app.quiz_configuration import get_quiz_settings


@pytest.fixture(autouse=True)
def _use_formal_quiz_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "get_quiz_settings", _formal_quiz_settings)


PASSWORD = SecretStr("A1z_" * 12)


def _install_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    monkeypatch.setattr(preparation, "Session", lambda _: session)
    monkeypatch.setattr(preparation, "_require_current_schema", lambda _: None)


def test_current_configuration_is_refused_before_preparation_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_load_test_bootstrap import _settings

    monkeypatch.setattr(bootstrap, "get_quiz_settings", get_quiz_settings)

    def refuse_connection(_: object) -> None:
        pytest.fail("configuration mismatch must be detected before resolving the DB host")

    monkeypatch.setattr(preparation, "_require_remote_database_host", refuse_connection)
    with pytest.raises(LoadTestBootstrapRefusedError, match="quiz_configuration_mismatch"):
        preparation.prepare_fresh_load_test_run(
            _settings(), _manifest(), preparation.RunOperatorCredential(password=PASSWORD)
        )


def test_fresh_preparation_inserts_only_single_run_and_secret_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    _install_session(monkeypatch, session)
    manifest = _manifest()
    result = preparation._insert_fresh_run(
        cast(Engine, FakeEngine()),
        manifest,
        preparation.RunOperatorCredential(password=PASSWORD),
    )
    assert session.committed
    assert session.scalar_calls == len(SQLModel.metadata.sorted_tables)
    assert result.created_events == 1
    assert len([row for row in session.added if isinstance(row, Event)]) == 1
    snapshots = [row for row in session.added if isinstance(row, EventQuizConfiguration)]
    assert len(snapshots) == 1
    assert snapshots[0].event_id == manifest.event.id
    assert len([row for row in session.added if isinstance(row, ScoringPolicy)]) == 1
    assert len([row for row in session.added if isinstance(row, DisplayNamePolicy)]) == 1
    accounts = [row for row in session.added if isinstance(row, AdminAccount)]
    assert len(accounts) == 1
    account = accounts[0]
    assert account.id == uuid5(manifest.run_id, "operator")
    assert account.username == preparation.run_operator_username(manifest.run_id)
    assert account.is_demo and not account.is_revoked
    assert create_operator_password_hasher().verify(
        account.password_hash, PASSWORD.get_secret_value()
    )
    assert PASSWORD.get_secret_value() not in result.model_dump_json()
    questions = [row for row in session.added if isinstance(row, Question)]
    assert len(questions) == 10
    assert all(row.image_url is None and row.image_alt is None for row in questions)


@pytest.mark.parametrize("collision_at", range(len(SQLModel.metadata.sorted_tables)))
def test_nonempty_application_table_refuses_without_any_insert(
    monkeypatch: pytest.MonkeyPatch,
    collision_at: int,
) -> None:
    session = FakeSession(collision_at=collision_at)
    _install_session(monkeypatch, session)
    with pytest.raises(LoadTestBootstrapRefusedError, match="database_not_empty"):
        preparation._insert_fresh_run(
            cast(Engine, FakeEngine()),
            _manifest(),
            preparation.RunOperatorCredential(password=PASSWORD),
        )
    assert session.rolled_back
    assert not session.added


@pytest.mark.parametrize("password", ["", "short", "a" * 32, "A" * 32, "1" * 32, "あ" * 32])
def test_weak_credential_is_refused_without_exposure(password: str) -> None:
    with pytest.raises(ValidationError) as failure:
        preparation.RunOperatorCredential(password=SecretStr(password))
    assert "input_value" not in str(failure.value)


def test_schema_failure_precedes_empty_check_and_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    _install_session(monkeypatch, session)

    def refuse_schema(_: object) -> None:
        raise LoadTestBootstrapRefusedError("schema_revision_mismatch")

    monkeypatch.setattr(preparation, "_require_current_schema", refuse_schema)
    with pytest.raises(LoadTestBootstrapRefusedError, match="schema_revision_mismatch"):
        preparation._insert_fresh_run(
            cast(Engine, FakeEngine()),
            _manifest(),
            preparation.RunOperatorCredential(password=PASSWORD),
        )
    assert session.scalar_calls == 0
    assert not session.added
