from __future__ import annotations

import ast
import base64
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from inspect import getsource
from textwrap import dedent
from typing import Self, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.config import AppEnvironment, Settings
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    ValidatedDependencyHost,
)
from quiz_app.load_test import bootstrap as bootstrap_module
from quiz_app.load_test.bootstrap import (
    REQUIRED_LOAD_TEST_TABLES,
    LoadTestBootstrapRefusedError,
    bootstrap_load_test_event,
    validate_bootstrap_settings,
)
from quiz_app.load_test.manifest import LoadTestManifest, build_load_test_manifest
from quiz_app.models import (
    DisplayNamePolicy,
    Event,
    EventQuizConfiguration,
    Question,
    QuestionOption,
    QuizRound,
    ScoringPolicy,
)
from quiz_app.models.enums import EventState, RoundState
from quiz_app.quiz_configuration import QuizSettings, get_quiz_settings
from quiz_app.schema_revision import SCHEMA_REVISION

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
IMAGE = f"registry.aws.internal/quiz/api@sha256:{'1' * 64}"
VALID_AWS_VALUE = f"Aa0_{secrets.token_urlsafe(40)}"
LOAD_TEST_BACKUP_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _manifest() -> LoadTestManifest:
    created_at = datetime(2099, 9, 3, 0, 0, tzinfo=UTC)
    return build_load_test_manifest(
        run_id=RUN_ID,
        event_id=EVENT_ID,
        event_slug="load-test-2026-09-03",
        scheduled_start_at=created_at + timedelta(hours=1),
        public_origin="https://quiz.aws.internal",
        source_commit="a" * 40,
        api_image=IMAGE,
        gateway_image=IMAGE,
        generator_image=IMAGE,
        scoring_policy_version=get_quiz_settings().scoring.version,
        scoring_base_points=400,
        scoring_initial_speed_bonus=100,
        scoring_speed_bucket_ms=100,
        scoring_speed_decrement_per_bucket=1,
        scoring_standard_duration_ms=10_000,
        scoring_is_approved=True,
        display_name_policy_version="display-v1",
        display_notice_version="notice-v1",
        display_max_code_points=20,
        display_reject_patterns_json="[]",
        created_at=created_at,
    )


def _formal_quiz_settings() -> QuizSettings:
    payload = get_quiz_settings().model_dump(mode="json")
    payload["timing"] = {
        "basis": "server_received_v1",
        "arrival_grace_ms": 0,
        "clock_tolerance_ms": 0,
        "client_ahead_tolerance_ms": 0,
    }
    payload["start_delay_seconds"] = 5
    payload["visibility"] = {
        target: {"mode": "manual", "initially_visible": visible, "changes": []}
        for target, visible in (
            ("distribution", True),
            ("personal_ranking", False),
            ("overall_ranking", False),
        )
    }
    return QuizSettings.model_validate_json(json.dumps(payload))


@pytest.fixture(autouse=True)
def _use_formal_quiz_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap_module, "get_quiz_settings", _formal_quiz_settings)


@pytest.mark.parametrize("mismatch", ["legacy_scoring", "new_runtime", "client_timing"])
def test_configuration_mismatch_is_refused_before_database_host_resolution(
    monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    manifest = _manifest()
    if mismatch == "legacy_scoring":
        scoring = manifest.event.scoring_policy.model_copy(
            update={
                "base_points": 10_000,
                "initial_speed_bonus": 500,
                "speed_decrement_per_bucket": 10,
            }
        )
        manifest = manifest.model_copy(
            update={"event": manifest.event.model_copy(update={"scoring_policy": scoring})}
        )
    elif mismatch == "new_runtime":
        monkeypatch.setattr(bootstrap_module, "get_quiz_settings", get_quiz_settings)
    else:
        client_settings = _formal_quiz_settings().model_copy(
            update={"timing": get_quiz_settings().timing}
        )
        monkeypatch.setattr(bootstrap_module, "get_quiz_settings", lambda: client_settings)

    def refuse_connection(_: Settings) -> None:
        pytest.fail("configuration mismatch must be detected before resolving the DB host")

    monkeypatch.setattr(bootstrap_module, "_require_remote_database_host", refuse_connection)
    with pytest.raises(LoadTestBootstrapRefusedError, match="quiz_configuration_mismatch"):
        bootstrap_load_test_event(_settings(), manifest)


def _settings(**overrides: object) -> Settings:
    configuration: dict[str, object] = {
        "app_env": AppEnvironment.AWS_DEMO,
        "app_version": "a" * 40,
        "database_url": (
            f"postgresql+psycopg://load_user:{VALID_AWS_VALUE}@db.aws.internal:5432/load"
            "?sslmode=verify-full"
        ),
        "redis_url": f"rediss://load_user:{VALID_AWS_VALUE}@cache.aws.internal:6379/0",
        "demo_operator_username": "managed-in-database",
        "demo_operator_password": "unused",
        "session_secret": VALID_AWS_VALUE,
        "enable_demo_auth": True,
        "enable_demo_seed": False,
        "session_cookie_secure": True,
        "operator_session_ttl_minutes": 120,
        "participant_session_ttl_minutes": 120,
        "realtime_allowed_origins": ["https://quiz.aws.internal"],
        "postgres_ssl_root_cert": "/run/secrets/aws-demo-postgres-ca.pem",
        "api_instance_id": "api-primary",
        "round_backup_storage_mode": "filesystem",
        "round_backup_directory": "/var/lib/quiz-round-backups",
        "round_backup_encryption_key": LOAD_TEST_BACKUP_KEY,
        "aws_demo_expected_event_id": EVENT_ID,
        "aws_demo_round_scope": "load-test-ten-round",
        "_env_file": None,
    }
    configuration.update(overrides)
    return Settings(**configuration)


class FakeScalarResult:
    def scalars(self) -> list[str]:
        return [SCHEMA_REVISION]


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class FakeConnection:
    def execute(self, _: object) -> FakeScalarResult:
        return FakeScalarResult()


class FakeInspector:
    def get_table_names(self) -> list[str]:
        return sorted(REQUIRED_LOAD_TEST_TABLES)


class FakeTransaction:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __enter__(self) -> Self:
        return self

    def __exit__(self, error_type: object, _: object, __: object) -> None:
        self.session.committed = error_type is None
        self.session.rolled_back = error_type is not None


class FakeSession:
    def __init__(
        self,
        *,
        collision_at: int | None = None,
        approved: bool = True,
        display_max_code_points: int = 20,
        missing_policy: type[object] | None = None,
    ) -> None:
        self.collision_at = collision_at
        self.approved = approved
        self.display_max_code_points = display_max_code_points
        self.missing_policy = missing_policy
        self.added: list[object] = []
        self.committed = False
        self.rolled_back = False
        self.executed_sql: list[str] = []
        self.scalar_calls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _: object, __: object, ___: object) -> None:
        return None

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def execute(self, statement: object, _: object | None = None) -> None:
        self.executed_sql.append(str(statement))

    def connection(self) -> FakeConnection:
        return FakeConnection()

    def get(self, model_type: type[object], _: object) -> object | None:
        if model_type is self.missing_policy:
            return None
        if model_type is ScoringPolicy:
            return ScoringPolicy(
                version=get_quiz_settings().scoring.version,
                base_points=400,
                initial_speed_bonus=100,
                speed_bucket_ms=100,
                speed_decrement_per_bucket=1,
                standard_duration_ms=10_000,
                is_approved=self.approved,
            )
        if model_type is DisplayNamePolicy:
            return DisplayNamePolicy(
                version="display-v1",
                notice_version="notice-v1",
                max_code_points=self.display_max_code_points,
            )
        return None

    def scalar(self, _: object) -> object | None:
        current_call = self.scalar_calls
        self.scalar_calls += 1
        return EVENT_ID if current_call == self.collision_at else None

    def add(self, record: object) -> None:
        self.added.append(record)

    def flush(self) -> None:
        return None


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
) -> FakeEngine:
    engine = FakeEngine()
    validated_host = ValidatedDependencyHost(
        dependency="database",
        hostname="db.aws.internal",
        resolved_addresses=("10.0.0.8",),
        selected_address="10.0.0.8",
    )
    monkeypatch.setattr(bootstrap_module, "Session", lambda _: session)
    monkeypatch.setattr(bootstrap_module, "inspect", lambda _: FakeInspector())
    monkeypatch.setattr(
        bootstrap_module,
        "validate_remote_dependency_host",
        lambda *_: validated_host,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "create_pinned_postgres_engine",
        lambda *_: cast(Engine, engine),
    )
    return engine


def test_settings_accept_both_aws_round_scopes_but_bootstrap_requires_load_scope() -> None:
    manifest = _manifest()

    validate_bootstrap_settings(_settings(), manifest)
    single_round = _settings(
        aws_demo_round_scope="single-round",
        round_backup_storage_mode="disabled",
        round_backup_directory="/var/empty/quiz-round-backups",
        round_backup_encryption_key="disabled",
    )
    assert single_round.aws_demo_round_scope == "single-round"
    with pytest.raises(LoadTestBootstrapRefusedError, match="ten_round_scope"):
        validate_bootstrap_settings(single_round, manifest)


@pytest.mark.parametrize(
    "overrides",
    [
        {"aws_demo_expected_event_id": UUID("33333333-3333-4333-8333-333333333333")},
    ],
)
def test_bootstrap_requires_manifest_target_to_match_aws_settings(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(LoadTestBootstrapRefusedError, match="mismatch"):
        validate_bootstrap_settings(_settings(**overrides), _manifest())


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"app_version": "b" * 40}, "source_commit_mismatch"),
        (
            {"realtime_allowed_origins": ["https://another.aws.internal"]},
            "public_origin_mismatch",
        ),
    ],
)
def test_bootstrap_requires_manifest_artifact_and_origin_to_match_settings(
    overrides: dict[str, object],
    error_code: str,
) -> None:
    with pytest.raises(LoadTestBootstrapRefusedError, match=error_code):
        validate_bootstrap_settings(_settings(**overrides), _manifest())


def test_bootstrap_origin_match_allows_only_settings_trailing_slash_normalization() -> None:
    settings = _settings(
        realtime_allowed_origins=[
            "https://another.aws.internal",
            "https://quiz.aws.internal/",
        ]
    )

    validate_bootstrap_settings(settings, _manifest())


def test_bootstrap_inserts_only_fresh_event_questions_options_and_initial_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    engine = _install_fake_session(monkeypatch, session)

    result = bootstrap_load_test_event(_settings(), _manifest())

    assert session.committed is True
    assert session.rolled_back is False
    assert result.created_events == 1
    assert result.created_questions == 10
    assert result.created_options == 40
    assert result.created_rounds == 1
    assert len([record for record in session.added if isinstance(record, Event)]) == 1
    snapshots = [record for record in session.added if isinstance(record, EventQuizConfiguration)]
    assert len(snapshots) == 1
    assert snapshots[0].event_id == EVENT_ID
    assert QuizSettings.model_validate_json(snapshots[0].settings_json) == _formal_quiz_settings()
    questions = [record for record in session.added if isinstance(record, Question)]
    assert len(questions) == 10
    assert all(question.image_url is None and question.image_alt is None for question in questions)
    options = [record for record in session.added if isinstance(record, QuestionOption)]
    assert len(options) == 40
    assert sum(option.is_correct for option in options) == 10
    assert all(option.label == "B" for option in options if option.is_correct)
    rounds = [record for record in session.added if isinstance(record, QuizRound)]
    assert len(rounds) == 1
    assert rounds[0].state is RoundState.PREPARING_ASSETS
    event = next(record for record in session.added if isinstance(record, Event))
    assert event.state is EventState.LOBBY_OPEN
    assert session.executed_sql[:3] == [
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '30s'",
        "SELECT pg_advisory_xact_lock(:lock_key)",
    ]
    assert all(sql.startswith(("SELECT", "SET LOCAL")) for sql in session.executed_sql)
    assert engine.disposed is True


def test_bootstrap_implementation_has_no_update_delete_or_reset_operation() -> None:
    module_tree = ast.parse(getsource(bootstrap_module))
    forbidden_calls = {
        "bulk_insert_mappings",
        "bulk_save_objects",
        "bulk_update_mappings",
        "create_all",
        "delete",
        "drop_all",
        "merge",
        "update",
    }
    actual_calls = {
        node.func.attr
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert actual_calls.isdisjoint(forbidden_calls)
    forbidden_sql_token = re.compile(r"\b(?:UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE)\b")
    string_constants = (
        node.value
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert all(forbidden_sql_token.search(value.upper()) is None for value in string_constants)

    insert_tree = ast.parse(dedent(getsource(bootstrap_module._insert_manifest_records)))
    session_calls = {
        node.func.attr
        for node in ast.walk(insert_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session"
    }
    assert session_calls == {"add", "flush"}


@pytest.mark.parametrize("collision_at", range(4))
def test_bootstrap_refuses_any_identity_collision_without_inserting(
    monkeypatch: pytest.MonkeyPatch,
    collision_at: int,
) -> None:
    session = FakeSession(collision_at=collision_at)
    engine = _install_fake_session(monkeypatch, session)

    with pytest.raises(LoadTestBootstrapRefusedError, match="identity_collision"):
        bootstrap_load_test_event(_settings(), _manifest())

    assert session.added == []
    assert session.committed is False
    assert session.rolled_back is True
    assert engine.disposed is True


@pytest.mark.parametrize(
    ("missing_policy", "error_code"),
    [
        (ScoringPolicy, "scoring_policy_missing"),
        (DisplayNamePolicy, "display_policy_missing"),
    ],
)
def test_bootstrap_refuses_missing_referenced_policy(
    monkeypatch: pytest.MonkeyPatch,
    missing_policy: type[object],
    error_code: str,
) -> None:
    session = FakeSession(missing_policy=missing_policy)
    _install_fake_session(monkeypatch, session)

    with pytest.raises(LoadTestBootstrapRefusedError, match=error_code):
        bootstrap_load_test_event(_settings(), _manifest())

    assert session.added == []
    assert session.rolled_back is True


def test_bootstrap_refuses_policy_field_mismatch_without_inserting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(approved=False)
    _install_fake_session(monkeypatch, session)

    with pytest.raises(LoadTestBootstrapRefusedError, match="mismatch:is_approved"):
        bootstrap_load_test_event(_settings(), _manifest())

    assert session.added == []
    assert session.rolled_back is True


def test_bootstrap_refuses_legacy_policy_with_client_ahead_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    original_get = session.get

    def get_policy(model_type: type[object], identity: object) -> object | None:
        value = original_get(model_type, identity)
        if isinstance(value, ScoringPolicy):
            value.client_ahead_tolerance_ms = 2500
        return value

    monkeypatch.setattr(session, "get", get_policy)
    _install_fake_session(monkeypatch, session)
    with pytest.raises(LoadTestBootstrapRefusedError, match="mismatch:timing"):
        bootstrap_load_test_event(_settings(), _manifest())
    assert session.added == []
    assert session.rolled_back is True


def test_bootstrap_refuses_display_policy_field_mismatch_without_inserting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(display_max_code_points=40)
    _install_fake_session(monkeypatch, session)

    with pytest.raises(LoadTestBootstrapRefusedError, match="mismatch:max_code_points"):
        bootstrap_load_test_event(_settings(), _manifest())

    assert session.added == []
    assert session.rolled_back is True


def test_bootstrap_refuses_schema_revision_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    _install_fake_session(monkeypatch, session)
    monkeypatch.setattr(FakeScalarResult, "scalars", lambda _: ["older_revision"])

    with pytest.raises(LoadTestBootstrapRefusedError, match="revision_mismatch"):
        bootstrap_load_test_event(_settings(), _manifest())

    assert session.added == []
    assert session.rolled_back is True


def test_bootstrap_revalidates_future_schedule_with_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    engine = _install_fake_session(monkeypatch, session)
    manifest = _manifest()

    with pytest.raises(LoadTestBootstrapRefusedError, match="schedule_not_future"):
        bootstrap_load_test_event(
            _settings(),
            manifest,
            clock=lambda: manifest.scheduled_start_at,
        )

    assert session.executed_sql == []
    assert session.added == []
    assert engine.disposed is False


def test_bootstrap_refuses_unsafe_database_resolution_before_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    engine = _install_fake_session(monkeypatch, session)

    def reject_database(*_: object) -> None:
        raise DependencyHostValidationError("database")

    monkeypatch.setattr(
        bootstrap_module,
        "validate_remote_dependency_host",
        reject_database,
    )

    with pytest.raises(LoadTestBootstrapRefusedError, match="database_host_invalid"):
        bootstrap_load_test_event(_settings(), _manifest())

    assert session.executed_sql == []
    assert session.added == []
    assert engine.disposed is False


def test_bootstrap_disposes_pinned_engine_when_transaction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    engine = _install_fake_session(monkeypatch, session)

    def fail_transaction(*_: object) -> None:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(bootstrap_module, "_insert_load_test_event", fail_transaction)

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        bootstrap_load_test_event(_settings(), _manifest())

    assert engine.disposed is True
