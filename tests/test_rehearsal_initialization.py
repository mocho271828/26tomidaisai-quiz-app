import json
import subprocess
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from test_load_test_bootstrap import EVENT_ID, VALID_AWS_VALUE, FakeEngine, _settings
from test_rehearsal_preparation import RehearsalSession

from quiz_app.config import Settings
from quiz_app.dependency_hosts import ValidatedDependencyHost
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.scoring import assign_speed_ranks, score_answer
from quiz_app.domain.types import AnswerScoreInput, ScoringRule
from quiz_app.models import AdminAccount, DisplayNamePolicy, ScoringPolicy
from quiz_app.operator.auth_service import create_operator_password_hasher
from quiz_app.rehearsal import initialization
from quiz_app.rehearsal.initialization import SCORING_POLICY_VERSION, RehearsalInitializationSecrets
from quiz_app.rehearsal.preparation import RehearsalEventInput, RehearsalPreparationRefusedError
from validation import rehearsal_initialize

ROOT = Path(__file__).resolve().parents[1]
CA = Path("/srv/rehearsal-ca/postgres.pem")
APP_URL = (
    f"postgresql+psycopg://rehearsal_app:{quote(VALID_AWS_VALUE, safe='')}"
    "@db.aws.internal:5432/load?sslmode=verify-full"
)
ADMIN_URL = APP_URL.replace("rehearsal_app:", "rehearsal_admin:")
HOST = ValidatedDependencyHost("database", "db.aws.internal", ("10.0.1.15",), "10.0.1.15")


def _secrets() -> RehearsalInitializationSecrets:
    return RehearsalInitializationSecrets(
        admin_database_url=SecretStr(ADMIN_URL),
        app_database_url=SecretStr(APP_URL),
        operator_username="rehearsal-operator",
        operator_password=SecretStr(VALID_AWS_VALUE),
    )


def _event() -> RehearsalEventInput:
    return RehearsalEventInput(
        event_id=EVENT_ID,
        title="ゲストクイズ",
        slug="quiz-rehearsal",
        scoring_policy_version=SCORING_POLICY_VERSION,
        display_name_policy_version="demo-display-name-v1",
    )


def _runtime() -> Settings:
    return _settings(database_url=APP_URL, aws_demo_round_scope="rehearsal-six-round")


@pytest.mark.parametrize(
    "field,value",
    [
        ("admin_database_url", "postgresql+psycopg://postgres:weak@localhost/quiz"),
        ("admin_database_url", ADMIN_URL.replace("db.aws.internal", "other.aws.internal")),
        ("admin_database_url", APP_URL),
        ("app_database_url", APP_URL.replace("rehearsal_app", "other_app")),
        ("operator_username", "demo-operator"),
        ("operator_password", "secret-not-valid"),
    ],
)
def test_invalid_credentials_refuse_without_input_exposure(field: str, value: str) -> None:
    values = _secrets().model_dump()
    values[field] = value if field == "operator_username" else SecretStr(value)
    with pytest.raises(ValidationError) as error:
        RehearsalInitializationSecrets.model_validate(values)
    assert "input_value" not in str(error.value)


def test_secret_json_accepts_strings_without_exposing_secrets() -> None:
    credentials = RehearsalInitializationSecrets.model_validate_json(
        json.dumps(
            {
                "admin_database_url": ADMIN_URL,
                "app_database_url": APP_URL,
                "operator_username": "rehearsal-operator",
                "operator_password": VALID_AWS_VALUE,
            }
        )
    )
    assert credentials.app_database_url.get_secret_value() == APP_URL
    assert VALID_AWS_VALUE not in repr(credentials)


def test_cli_requires_human_policy_confirmation_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize = Mock(side_effect=AssertionError("must not initialize"))
    monkeypatch.setattr(rehearsal_initialize, "_initialize", initialize)
    with pytest.raises(SystemExit) as failure:
        rehearsal_initialize.main(
            [
                "--content",
                "content.json",
                "--event-file",
                "event.json",
                "--release-file",
                "release.env",
                "--repository-root",
                str(ROOT),
                "--secrets-file",
                "secrets.json",
                "--confirm-event-id",
                str(EVENT_ID),
                "--expected-content-sha256",
                "a" * 64,
            ]
        )
    assert failure.value.code == 2
    initialize.assert_not_called()


def test_migration_uses_exact_legacy_command_private_dotenv_and_pinned_ca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directories: list[Path] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        assert command[-2:] == ["upgrade", "head"]
        assert command[-3] == str(ROOT / "backend/alembic.ini")
        assert options["capture_output"] is True and options["timeout"] == 120
        directory = options["cwd"]
        assert isinstance(directory, Path)
        directories.append(directory)
        assert (directory / ".env").stat().st_mode & 0o777 == 0o600
        assert (directory / "pg_service.conf").stat().st_mode & 0o777 == 0o600
        environment = options["env"]
        assert environment == {
            "PGSERVICE": "rehearsal-initialization",
            "PGSERVICEFILE": str(directory / "pg_service.conf"),
        }
        runtime = Settings(_env_file=directory / ".env")
        assert runtime.database_url == ADMIN_URL
        assert runtime.app_env.value == "aws-demo"
        service = (directory / "pg_service.conf").read_text()
        assert "hostaddr=10.0.1.15" in service and f"sslrootcert={CA}" in service
        assert "connect_timeout=5" in service and "statement_timeout=30000" in service
        assert VALID_AWS_VALUE not in service
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(initialization.subprocess, "run", run)
    initialization.run_existing_migrations(ROOT, _runtime(), _secrets(), CA, HOST)
    assert not directories[0].exists()


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[RehearsalSession, RehearsalSession, Mock]:
    fresh, initialized = RehearsalSession(), RehearsalSession()
    sessions = iter([fresh, initialized])
    monkeypatch.setattr(initialization, "Session", lambda _: next(sessions))
    monkeypatch.setattr(initialization, "create_engine", Mock(return_value=FakeEngine()))
    monkeypatch.setattr(initialization, "validate_remote_dependency_host", Mock(return_value=HOST))
    monkeypatch.setattr(initialization, "_require_admin_identity", Mock())
    monkeypatch.setattr(initialization, "_require_current_schema", Mock())
    monkeypatch.setattr(initialized, "connection", Mock(return_value=Mock()))
    migration = Mock()
    monkeypatch.setattr(initialization, "run_existing_migrations", migration)
    return fresh, initialized, migration


def _initialize() -> None:
    initialization.initialize_rehearsal_database(
        _runtime(),
        _event(),
        _secrets(),
        repository_root=ROOT,
        postgres_ca_file=CA,
    )


def test_initialization_inserts_new_policy_and_operator_after_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh, initialized, migration = _install(monkeypatch)
    _initialize()
    assert fresh.committed and initialized.committed
    migration.assert_called_once()
    assert {type(row) for row in initialized.added} == {
        ScoringPolicy,
        DisplayNamePolicy,
        AdminAccount,
    }
    policy = next(row for row in initialized.added if isinstance(row, ScoringPolicy))
    assert (
        policy.version,
        policy.base_points,
        policy.initial_speed_bonus,
        policy.speed_bucket_ms,
        policy.speed_decrement_per_bucket,
        policy.standard_duration_ms,
        policy.is_approved,
    ) == (
        SCORING_POLICY_VERSION,
        400,
        100,
        100,
        1,
        10_000,
        True,
    )
    account = next(row for row in initialized.added if isinstance(row, AdminAccount))
    assert account.is_demo and not account.is_revoked
    assert create_operator_password_hasher().verify(account.password_hash, VALID_AWS_VALUE)
    calls = initialized.connection().exec_driver_sql.call_args_list
    statements = [call.args[0] for call in calls]
    assert statements[0].startswith('CREATE ROLE "rehearsal_app" LOGIN NOSUPERUSER NOCREATEDB')
    assert any(
        "GRANT SELECT ON TABLE public.alembic_version" in statement for statement in statements
    )
    assert not any(
        'UPDATE, DELETE ON TABLE public."alembic_version"' in statement for statement in statements
    )
    assert all(call.kwargs["execution_options"] == {"no_parameters": True} for call in calls)


def test_nonfresh_database_never_migrates(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh, initialized, migration = _install(monkeypatch)
    monkeypatch.setattr(fresh, "scalar", Mock(return_value=True))
    with pytest.raises(RehearsalPreparationRefusedError, match="database_not_fresh"):
        _initialize()
    migration.assert_not_called()
    assert not initialized.added


def test_participant_id_pool_grants_only_select_without_changing_other_table_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, initialized, _ = _install(monkeypatch)
    _initialize()
    statements = [call.args[0] for call in initialized.connection().exec_driver_sql.call_args_list]

    for table in ("participant_id_pools", "participant_id_slots"):
        grants = [
            statement for statement in statements if f'ON TABLE public."{table}"' in statement
        ]
        assert grants == [f'GRANT SELECT ON TABLE public."{table}" TO "rehearsal_app"']
    for table in ("participants", "participant_sessions", "answers"):
        assert (
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public."{table}" TO "rehearsal_app"'
            in statements
        )


def test_existing_application_data_after_migration_never_creates_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, initialized, _ = _install(monkeypatch)
    monkeypatch.setattr(initialized, "scalar", Mock(return_value=True))
    with pytest.raises(RehearsalPreparationRefusedError, match="application_not_empty"):
        _initialize()
    initialized.connection().exec_driver_sql.assert_not_called()
    assert not initialized.added


@pytest.mark.parametrize(
    "failure",
    [
        SQLAlchemyError("secret-marker"),
        subprocess.CalledProcessError(1, "alembic", stderr=b"secret-marker"),
    ],
)
def test_initialization_failure_is_redacted_and_never_continues(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    _, initialized, migration = _install(monkeypatch)
    migration.side_effect = failure
    with pytest.raises(RehearsalPreparationRefusedError, match="inspect_database") as error:
        _initialize()
    assert "secret-marker" not in str(error.value) and not initialized.added


@pytest.mark.parametrize(
    "elapsed_ms,total",
    [
        (0, 120),
        (499, 120),
        (500, 119),
        (999, 119),
        (1000, 118),
        (8999, 103),
        (9000, 102),
        (9499, 102),
        (9500, 101),
        (9999, 101),
    ],
)
def test_official_scoring_half_second_boundaries(elapsed_ms: int, total: int) -> None:
    rule = ScoringRule("scoring-2026-09-20-v1", 100, 20, 500, 1, 10_000)
    answer = AnswerScoreInput(UUID(int=1), "AAA", True, elapsed_ms)
    assert score_answer(rule, answer).total_points == total


def test_official_scoring_close_wrong_and_bucket_ties() -> None:
    rule = ScoringRule("scoring-2026-09-20-v1", 100, 20, 500, 1, 10_000)
    with pytest.raises(DomainInvariantError, match="outside_scoring_window"):
        score_answer(rule, AnswerScoreInput(UUID(int=1), "AAA", True, 10_000))
    wrong = score_answer(rule, AnswerScoreInput(UUID(int=1), "AAA", False, 0))
    assert wrong.total_points == 0
    scores = [
        score_answer(rule, AnswerScoreInput(UUID(int=i), f"AA{i}", True, ms))
        for i, ms in enumerate([0, 499, 500], start=1)
    ]
    assert [score.speed_rank for score in assign_speed_ranks(scores)] == [1, 1, 3]
