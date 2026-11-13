from dataclasses import dataclass
from pathlib import Path
from typing import Self
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import SQLModel
from test_load_test_bootstrap import EVENT_ID, FakeEngine, FakeSession, _settings

from quiz_app.config import LOCAL_DEMO_EVENT_ID, AppEnvironment
from quiz_app.dependency_hosts import DependencyHostValidationError, ValidatedDependencyHost
from quiz_app.models import Event, EventQuizConfiguration, Question, QuestionOption, QuizRound
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.operator.auth_service import create_operator_password_hasher
from quiz_app.quiz_configuration import configured_scoring_policy, get_quiz_settings
from quiz_app.rehearsal import preparation
from quiz_app.rehearsal.content import RehearsalContent, load_rehearsal_content
from quiz_app.rehearsal.preparation import RehearsalEventInput, RehearsalPreparationRefusedError
from quiz_app.schema_revision import SCHEMA_REVISION

CONTENT_FILE = Path(__file__).resolve().parents[1] / "content/event-questions.json"
HOST_CA_FILE = Path("/srv/quiz-secrets/approved-postgres-ca.pem")
OPERATOR_HASH = create_operator_password_hasher().hash("test-only-prepared-operator")


def _event() -> RehearsalEventInput:
    return RehearsalEventInput(
        event_id=EVENT_ID,
        title="ゲストクイズ",
        slug="quiz-rehearsal",
        scoring_policy_version=get_quiz_settings().scoring.version,
        display_name_policy_version="display-v1",
    )


@dataclass
class MappingResult:
    rows: list[dict[str, object]]

    def mappings(self) -> Self:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


class RehearsalSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.blocked_table: str | None = None
        self.table_names = {"alembic_version", *SQLModel.metadata.tables}
        self.revision_rows: list[dict[str, object]] = [{"version_num": SCHEMA_REVISION}]
        self.scoring_rows: list[dict[str, object]] = [
            configured_scoring_policy(get_quiz_settings()).model_dump(exclude={"created_at"})
        ]
        self.display_rows: list[dict[str, object]] = [{"version": "display-v1"}]
        self.operator_rows: list[dict[str, object]] = [
            {
                "username": "event-rehearsal-operator",
                "password_hash": OPERATOR_HASH,
                "role": "Operator",
                "is_demo": True,
                "is_revoked": False,
            }
        ]

    def execute(self, statement: object, _: object | None = None) -> MappingResult:
        query = str(statement)
        self.executed_sql.append(query)
        if "FROM alembic_version" in query:
            return MappingResult(self.revision_rows)
        if "FROM scoring_policies" in query:
            return MappingResult(self.scoring_rows)
        if "FROM display_name_policies" in query:
            return MappingResult(self.display_rows)
        if "FROM admin_accounts" in query:
            return MappingResult(self.operator_rows)
        return MappingResult([])

    def scalar(self, statement: object) -> bool:
        self.scalar_calls += 1
        self.executed_sql.append(str(statement))
        return self.blocked_table is not None and f"FROM {self.blocked_table}" in str(statement)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    session: RehearsalSession,
) -> tuple[FakeEngine, Mock]:
    engine = FakeEngine()
    engine_factory = Mock(return_value=engine)
    monkeypatch.setattr(preparation, "create_engine", engine_factory)
    monkeypatch.setattr(preparation, "Session", lambda _: session)
    monkeypatch.setattr(
        preparation, "inspect", lambda _: Mock(get_table_names=lambda: sorted(session.table_names))
    )
    monkeypatch.setattr(
        preparation,
        "validate_remote_dependency_host",
        lambda *_: ValidatedDependencyHost(
            dependency="database",
            hostname="db.aws.internal",
            resolved_addresses=("10.0.1.15",),
            selected_address="10.0.1.15",
        ),
    )
    return engine, engine_factory


def _prepare(content: RehearsalContent | None = None) -> preparation.RehearsalPreparationResult:
    return preparation.prepare_rehearsal_event(
        _settings(aws_demo_round_scope="rehearsal-six-round"),
        _event(),
        content or load_rehearsal_content(CONTENT_FILE),
        postgres_ca_file=HOST_CA_FILE,
    )


@pytest.mark.parametrize(
    "selection",
    [
        (1, 2, 3, 4, 5, 6),
        (6, 5, 4, 3, 2, 1),
        (1, 2, 3),
        (12, 2, 11, 3, 4, 5, 6, 1),
        tuple(range(1, 11)),
    ],
)
def test_selected_questions_and_one_round_insert_without_mutating_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    selection: tuple[int, ...],
) -> None:
    session = RehearsalSession()
    engine, engine_factory = _install(monkeypatch, session)
    content = load_rehearsal_content(CONTENT_FILE)
    next_question_id = max(question.id for question in content.questions) + 1
    questions = content.questions + tuple(
        content.questions[0].model_copy(update={"id": question_id})
        for question_id in range(next_question_id, max(selection) + 1)
    )
    content = RehearsalContent(
        **content.model_dump(exclude={"selected_question_ids", "questions"}),
        questions=questions,
        selected_question_ids=selection,
    )
    result = _prepare(content)
    assert session.committed and not session.rolled_back and engine.disposed
    assert result.event_id == EVENT_ID
    assert (
        result.created_events,
        result.created_questions,
        result.created_options,
        result.created_rounds,
    ) == (1, len(selection), len(selection) * 4, 1)
    assert {type(record) for record in session.added} == {
        Event,
        EventQuizConfiguration,
        Question,
        QuestionOption,
        QuizRound,
    }
    events = [row for row in session.added if isinstance(row, Event)]
    questions = [row for row in session.added if isinstance(row, Question)]
    options = [row for row in session.added if isinstance(row, QuestionOption)]
    rounds = [row for row in session.added if isinstance(row, QuizRound)]
    assert len(events) == len(rounds) == 1
    assert events[0].state == EventState.LOBBY_OPEN
    assert events[0].scoring_policy_version == get_quiz_settings().scoring.version
    assert events[0].display_name_policy_version == "display-v1"
    assert len(questions) == len(selection) and len(options) == len(selection) * 4
    assert [row.prompt for row in questions] == [
        q.display_prompt for q in content.selected_questions
    ]
    assert [row.version for row in questions] == list(range(1, len(selection) + 1))
    for question, source in zip(questions, content.selected_questions, strict=True):
        assert question.event_id == EVENT_ID
        assert question.explanation == source.explanation
        assert question.image_url is None and question.image_alt is None
        correct_options = [o for o in options if o.question_id == question.id and o.is_correct]
        assert len(correct_options) == 1
        assert correct_options[0].label == source.correct_label
    assert rounds[0].question_id == questions[0].id
    assert rounds[0].state == RoundState.PREPARING_ASSETS
    assert rounds[0].result_publication == RoundResultPublication.WITHHELD
    assert all(
        row.id.version == 4 for row in session.added if not isinstance(row, EventQuizConfiguration)
    )
    assert (
        len({row.id for row in session.added if not isinstance(row, EventQuizConfiguration)})
        == 2 + len(selection) * 5
    )
    configurations = [row for row in session.added if isinstance(row, EventQuizConfiguration)]
    assert len(configurations) == 1 and configurations[0].event_id == EVENT_ID
    assert configurations[0].settings_json == get_quiz_settings().model_dump_json()
    assert all(sql.startswith(("SET LOCAL", "SELECT")) for sql in session.executed_sql)
    assert session.executed_sql[:3] == [
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '30s'",
        "SELECT pg_advisory_xact_lock(:lock_key)",
    ]
    assert engine_factory.call_args.kwargs["connect_args"] == {
        "host": "db.aws.internal",
        "hostaddr": "10.0.1.15",
        "connect_timeout": 5,
        "sslrootcert": str(HOST_CA_FILE),
    }


@pytest.mark.parametrize(
    "blocked_table",
    [name for name in SQLModel.metadata.tables if name not in preparation.PREPARED_TABLES],
)
def test_existing_event_or_any_application_data_refuses_before_insert(
    monkeypatch: pytest.MonkeyPatch,
    blocked_table: str,
) -> None:
    session = RehearsalSession()
    session.blocked_table = blocked_table
    engine, _ = _install(monkeypatch, session)
    with pytest.raises(RehearsalPreparationRefusedError, match="database_not_empty"):
        _prepare()
    assert session.rolled_back and not session.committed and engine.disposed
    assert not session.added


@pytest.mark.parametrize("revisions", [[], ["old-revision"], [SCHEMA_REVISION, SCHEMA_REVISION]])
def test_unknown_schema_revision_refuses_before_application_queries(
    monkeypatch: pytest.MonkeyPatch,
    revisions: list[str],
) -> None:
    session = RehearsalSession()
    session.revision_rows = [{"version_num": value} for value in revisions]
    _install(monkeypatch, session)
    with pytest.raises(RehearsalPreparationRefusedError, match="schema_revision_mismatch"):
        _prepare()
    assert session.scalar_calls == 0 and not session.added and session.rolled_back


def test_missing_schema_is_not_created(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RehearsalSession()
    session.table_names.remove("participants")
    _install(monkeypatch, session)
    with pytest.raises(RehearsalPreparationRefusedError, match="schema_missing"):
        _prepare()
    assert session.scalar_calls == 0 and not session.added


@pytest.mark.parametrize(
    "row_attribute, rows",
    [
        ("scoring_rows", []),
        ("scoring_rows", [{"version": "scoring-v1", "is_approved": False}]),
        ("scoring_rows", [{"version": "scoring-v1", "is_approved": 1}]),
        ("scoring_rows", [{"version": "other-policy", "is_approved": True}]),
        ("display_rows", []),
        ("display_rows", [{"version": "other-policy"}]),
        ("operator_rows", []),
    ],
)
def test_missing_or_unapproved_prerequisites_refuse(
    monkeypatch: pytest.MonkeyPatch,
    row_attribute: str,
    rows: list[dict[str, object]],
) -> None:
    session = RehearsalSession()
    setattr(session, row_attribute, rows)
    _install(monkeypatch, session)
    with pytest.raises(RehearsalPreparationRefusedError):
        _prepare()
    assert session.rolled_back and not session.added


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("username", "demo-operator"),
        ("username", "LOCAL-rehearsal"),
        ("username", "operator@localhost"),
        ("is_demo", False),
        ("is_revoked", True),
        ("role", "Supervisor"),
        ("role", "UnexpectedRole"),
        ("password_hash", "secret-driver-value"),
        ("is_demo", "true"),
    ],
)
def test_invalid_operator_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: object,
) -> None:
    session = RehearsalSession()
    session.operator_rows[0][attribute] = value
    _install(monkeypatch, session)
    with pytest.raises(RehearsalPreparationRefusedError) as failure:
        _prepare()
    assert "secret-driver-value" not in str(failure.value)
    assert session.rolled_back and not session.added


@pytest.mark.parametrize(
    "configuration,error_code",
    [
        ({"app_env": AppEnvironment.LOCAL_DEMO}, "requires_aws_demo"),
        ({"aws_demo_round_scope": "single-round"}, "requires_six_round_scope"),
        ({"aws_demo_round_scope": "load-test-ten-round"}, "requires_six_round_scope"),
        ({"enable_demo_seed": True}, "requires_demo_seed_disabled"),
        ({"aws_demo_expected_event_id": uuid4()}, "event_id_mismatch"),
    ],
)
def test_settings_mismatch_refuses_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    configuration: dict[str, object],
    error_code: str,
) -> None:
    host_validator = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(preparation, "validate_remote_dependency_host", host_validator)
    settings = _settings(aws_demo_round_scope="rehearsal-six-round").model_copy(
        update=configuration
    )
    with pytest.raises(RehearsalPreparationRefusedError, match=error_code):
        preparation.prepare_rehearsal_event(
            settings, _event(), load_rehearsal_content(CONTENT_FILE), postgres_ca_file=HOST_CA_FILE
        )
    host_validator.assert_not_called()


@pytest.mark.parametrize("event_id", [LOCAL_DEMO_EVENT_ID, UUID(int=0)])
def test_local_or_nonrandom_event_identity_is_rejected(event_id: UUID) -> None:
    with pytest.raises(ValidationError, match="new_uuid4_event_id"):
        RehearsalEventInput.model_validate({**_event().model_dump(), "event_id": event_id})


def test_relative_ca_path_is_rejected_before_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    host_validator = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(preparation, "validate_remote_dependency_host", host_validator)
    with pytest.raises(RehearsalPreparationRefusedError, match="ca_path_not_absolute"):
        preparation.prepare_rehearsal_event(
            _settings(aws_demo_round_scope="rehearsal-six-round"),
            _event(),
            load_rehearsal_content(CONTENT_FILE),
            postgres_ca_file=Path("relative.pem"),
        )
    host_validator.assert_not_called()


def test_rejected_dns_never_creates_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine_factory = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(preparation, "create_engine", engine_factory)
    monkeypatch.setattr(
        preparation,
        "validate_remote_dependency_host",
        Mock(side_effect=DependencyHostValidationError("database")),
    )
    with pytest.raises(RehearsalPreparationRefusedError, match="database_host_invalid"):
        _prepare()
    engine_factory.assert_not_called()


@pytest.mark.parametrize(
    "failure,error_code",
    [
        (SQLAlchemyError("secret-driver-value"), "database_failed"),
        (
            IntegrityError("secret-query", {}, ValueError("secret-driver-value")),
            "concurrent_identity_collision",
        ),
    ],
)
def test_database_failure_rolls_back_and_redacts_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: SQLAlchemyError,
    error_code: str,
) -> None:
    session = RehearsalSession()
    engine, _ = _install(monkeypatch, session)
    monkeypatch.setattr(session, "flush", Mock(side_effect=failure))
    with pytest.raises(RehearsalPreparationRefusedError, match=error_code) as result:
        _prepare()
    assert "secret" not in str(result.value)
    assert session.rolled_back and not session.committed and engine.disposed


def test_uuid_generation_does_not_reuse_previous_question_or_round_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = RehearsalSession()
    _install(monkeypatch, first)
    _prepare()
    second = RehearsalSession()
    _install(monkeypatch, second)
    _prepare()
    first_ids = {
        row.id for row in first.added if not isinstance(row, (Event, EventQuizConfiguration))
    }
    assert first_ids.isdisjoint(
        row.id for row in second.added if not isinstance(row, (Event, EventQuizConfiguration))
    )


def test_cleanup_failure_after_commit_is_redacted_and_identifies_committed_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RehearsalSession()
    engine, _ = _install(monkeypatch, session)
    monkeypatch.setattr(engine, "dispose", Mock(side_effect=SQLAlchemyError("secret-cleanup")))
    with pytest.raises(
        RehearsalPreparationRefusedError, match="cleanup_failed_after_commit"
    ) as error:
        _prepare()
    assert session.committed and not session.rolled_back
    assert "secret-cleanup" not in str(error.value)


def test_cleanup_failure_preserves_prior_database_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RehearsalSession()
    session.blocked_table = "events"
    engine, _ = _install(monkeypatch, session)
    monkeypatch.setattr(engine, "dispose", Mock(side_effect=OSError("secret-cleanup")))
    with pytest.raises(RehearsalPreparationRefusedError, match="database_not_empty") as error:
        _prepare()
    assert session.rolled_back and not session.added
    assert "secret-cleanup" not in str(error.value)
