import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.engine import Row, RowMapping
from sqlalchemy.engine.result import SimpleResultMetaData
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import Session, SQLModel
from test_aws_review_host import inputs as inputs
from test_aws_review_host import secrets_fixture as secrets_fixture

from quiz_app.config import Settings
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.models import (
    AdminAccount,
    AdminSession,
    Answer,
    DisplayNameConsent,
    DisplayNamePolicy,
    Event,
    EventQuizConfiguration,
    IdempotencyRecord,
    OperationAudit,
    Participant,
    ParticipantSession,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    ReadinessReport,
    RoundOptionStat,
    RoundPreparationTarget,
    ScoreLedger,
)
from quiz_app.models.enums import EventState, RoundState, SpeedCohort
from quiz_app.models.participant_ids import ParticipantIdPool, ParticipantIdSlot
from quiz_app.operator.final_round import FinalRoundService
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
    require_event_configuration,
)
from quiz_app.quiz_configuration.contracts import AwardCeremonyPhase
from quiz_app.quiz_configuration.persistence import (
    set_award_ceremony_configuration,
    set_final_round_configuration,
)
from validation.aws_review import reset_data
from validation.aws_review.host_contracts import HostInput, HostRefusedError
from validation.aws_review.host_data import HostSecrets, runtime_settings

NOW = datetime(2026, 10, 21, 10, tzinfo=UTC)
EVENT_ID, ROUND_ID, QUESTION_ID, PARTICIPANT_ID, ACCOUNT_ID = (UUID(int=i) for i in range(1, 6))
RESET_ID = UUID(int=6)
OPERATOR_HASH = str(UUID(int=7))
CREDENTIAL_VERSION = "test-v1"


def row_mapping(values: dict[str, object]) -> RowMapping:
    """DBへ接続せず、SQLAlchemyが実際に返すMapping境界を再現する。"""
    return Row(
        SimpleResultMetaData(list(values)),
        None,
        {key: index for index, key in enumerate(values)},
        tuple(values.values()),
    )._mapping


@dataclass(frozen=True)
class Database:
    engine: Engine
    first_question_id: UUID


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Database]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
        connection.create_function("clock_timestamp", 0, lambda: NOW.isoformat())
        connection.execute("PRAGMA foreign_keys = ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(reset_data, "_require_database_ready", lambda _: None)
    monkeypatch.setattr(reset_data, "_require_available_participant_ids", lambda *_: 2)
    monkeypatch.setattr(reset_data, "_database_time", lambda _: NOW)
    settings = get_quiz_settings()
    first_question_id, option_id, snapshot_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():

        def add(record: SQLModel) -> None:
            session.add(record)
            session.flush()

        add(configured_scoring_policy(settings))
        add(DisplayNamePolicy(version="names-v1", notice_version="notice-v1"))
        add(
            AdminAccount(
                id=ACCOUNT_ID,
                username="rehearsal-operator",
                password_hash=OPERATOR_HASH,
                password_policy_version=CREDENTIAL_VERSION,
                is_demo=True,
            )
        )
        add(
            Event(
                id=EVENT_ID,
                slug="review-test",
                title="preserve-title",
                version=12,
                state=EventState.COMPLETED,
                scoring_policy_version=settings.scoring.version,
                display_name_policy_version="names-v1",
            )
        )
        configuration = initial_event_configuration(EVENT_ID, settings)
        configuration.last_applied_round = 8
        configuration.overall_ranking_visible = not configuration.overall_ranking_visible
        add(configuration)
        add(
            Question(id=QUESTION_ID, event_id=EVENT_ID, version=2, prompt="second", display_order=2)
        )
        add(
            Question(
                id=first_question_id, event_id=EVENT_ID, version=1, prompt="first", display_order=1
            )
        )
        add(
            QuestionOption(
                id=option_id,
                question_id=QUESTION_ID,
                label="A",
                content="preserve-option",
                display_order=0,
                is_correct=True,
            )
        )
        add(
            QuizRound(
                id=ROUND_ID,
                event_id=EVENT_ID,
                question_id=QUESTION_ID,
                version=40,
                state=RoundState.ROUND_BACKUP_VALIDATED,
            )
        )
        add(Participant(id=PARTICIPANT_ID, event_id=EVENT_ID, public_label="ABC"))
        add(
            DisplayNameConsent(
                participant_id=PARTICIPANT_ID, notice_version="notice-v1", policy_version="names-v1"
            )
        )
        add(
            ParticipantSession(
                participant_id=PARTICIPANT_ID,
                token_hash=uuid4().hex,
                expires_at=NOW + timedelta(hours=1),
            )
        )
        add(
            AdminSession(
                account_id=ACCOUNT_ID,
                token_hash=uuid4().hex,
                csrf_hash="old-csrf",
                issued_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
        )
        add(
            Answer(
                participant_id=PARTICIPANT_ID,
                round_id=ROUND_ID,
                option_id=option_id,
                client_answer_id=uuid4(),
                client_elapsed_ms=1000,
            )
        )
        add(
            ScoreLedger(
                participant_id=PARTICIPANT_ID,
                round_id=ROUND_ID,
                scoring_policy_version=settings.scoring.version,
                is_correct=True,
                base_points=100,
                speed_points=10,
                total_points=110,
                speed_cohort=SpeedCohort.CORRECT,
            )
        )
        add(
            ReadinessReport(
                participant_id=PARTICIPANT_ID, round_id=ROUND_ID, round_version=40, is_ready=True
            )
        )
        add(RoundPreparationTarget(participant_id=PARTICIPANT_ID, round_id=ROUND_ID))
        add(
            RoundOptionStat(
                round_id=ROUND_ID,
                option_id=option_id,
                valid_response_count=1,
                response_percentage=100,
            )
        )
        add(RankSnapshot(id=snapshot_id, event_id=EVENT_ID, round_id=ROUND_ID, version=1))
        add(
            RankSnapshotRow(
                snapshot_id=snapshot_id,
                public_label="ABC",
                overall_rank=1,
                total_points=110,
                correct_count=1,
            )
        )
        add(
            OperationAudit(
                event_id=EVENT_ID,
                round_id=ROUND_ID,
                account_id=ACCOUNT_ID,
                operation="complete_event",
                target_type="event",
                target_id=str(EVENT_ID),
                reason="preserve original in archive",
                outcome="succeeded",
            )
        )
        add(
            IdempotencyRecord(
                account_id=ACCOUNT_ID,
                idempotency_key=uuid4(),
                operation="complete_event",
                request_hash="old-request",
                response_status=200,
                response_json="{}",
                resulting_version=12,
                expires_at=NOW + timedelta(hours=1),
            )
        )
        add(ParticipantIdPool(event_id=EVENT_ID, capacity=3))
        for ordinal, label in enumerate(("ABC", "DEF", "GHI"), start=1):
            add(ParticipantIdSlot(event_id=EVENT_ID, ordinal=ordinal, public_label=label))
    yield Database(engine, first_question_id)
    engine.dispose()


def _reset(
    database: Database,
    callback: Callable[[Session], None],
    *,
    version: int = 12,
    event_id: UUID = EVENT_ID,
) -> reset_data.EventResetResult:
    with Session(database.engine) as session, session.begin():
        return reset_data._reset_in_transaction(session, event_id, version, RESET_ID, callback)


@pytest.mark.parametrize("has_final_round", [False, True])
@pytest.mark.parametrize("award_phase", ["Waiting", "Announced", "Finished"])
def test_reset_archives_before_deleting_and_preserves_identity_questions_and_id_pool(
    database: Database, has_final_round: bool, award_phase: AwardCeremonyPhase
) -> None:
    with Session(database.engine) as session, session.begin():
        configuration = require_event_configuration(session, session.get(Event, EVENT_ID))
        if has_final_round:
            set_final_round_configuration(configuration, ROUND_ID)
        configuration = require_event_configuration(session, session.get(Event, EVENT_ID))
        set_award_ceremony_configuration(configuration, award_phase)

    def archive(session: Session) -> None:
        assert session.get(Event, EVENT_ID).version == 12
        configuration = require_event_configuration(session, session.get(Event, EVENT_ID))
        assert configuration.final_round_id == (ROUND_ID if has_final_round else None)
        assert configuration.award_ceremony_phase == award_phase
        assert session.get(QuizRound, ROUND_ID) is not None
        assert session.get(Participant, PARTICIPANT_ID) is not None
        assert session.scalar(select(OperationAudit)).reason == "preserve original in archive"

    result = _reset(database, archive)
    assert result.previous_version == 12 and result.event_version == 13
    assert result.participant_ids_remaining == 2
    assert set(result.deleted.model_dump().values()) == {1}
    assert FinalRoundService(database.engine).get_final_round(EVENT_ID).final_round_id is None
    with Session(database.engine) as session:
        current = session.get(Event, EVENT_ID)
        assert current.version == 13 and current.state == EventState.LOBBY_OPEN
        assert current.title == "preserve-title" and current.slug == "review-test"
        assert session.get(AdminAccount, ACCOUNT_ID).password_hash == OPERATOR_HASH
        assert session.scalar(select(func.count()).select_from(Question)) == 2
        assert session.scalar(select(QuestionOption)).content == "preserve-option"
        assert session.get(ParticipantIdPool, EVENT_ID).capacity == 3
        assert session.scalar(select(func.count()).select_from(ParticipantIdSlot)) == 3
        configuration = session.get(EventQuizConfiguration, EVENT_ID)
        expected = initial_event_configuration(EVENT_ID, get_quiz_settings())
        assert configuration.model_dump() == expected.model_dump()
        current_round = session.scalar(select(QuizRound))
        assert current_round.id == result.new_round_id != ROUND_ID
        assert current_round.version == 1 and current_round.state == RoundState.PREPARING_ASSETS
        assert current_round.question_id == database.first_question_id
        for model in (
            Participant,
            ParticipantSession,
            DisplayNameConsent,
            AdminSession,
            Answer,
            ScoreLedger,
            ReadinessReport,
            RoundPreparationTarget,
            RoundOptionStat,
            RankSnapshot,
            RankSnapshotRow,
            IdempotencyRecord,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        audits = session.scalars(select(OperationAudit)).all()
        assert len(audits) == 1 and audits[0].operation == "reset_event"
        assert audits[0].idempotency_key == RESET_ID and audits[0].round_id is None
        assert reset_data.EventResetResult.model_validate_json(audits[0].reason) == result


@pytest.mark.parametrize("point", ["archive", "after-delete", "after-settings"])
@pytest.mark.parametrize("award_phase", ["Announced", "Finished"])
def test_any_failure_rolls_back_old_rows_and_progress(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    award_phase: AwardCeremonyPhase,
) -> None:
    with Session(database.engine) as session, session.begin():
        configuration = require_event_configuration(session, session.get(Event, EVENT_ID))
        set_final_round_configuration(configuration, ROUND_ID)
        configuration = require_event_configuration(session, session.get(Event, EVENT_ID))
        set_award_ceremony_configuration(configuration, award_phase)

    def fail(_session: Session) -> None:
        raise HostRefusedError("test_failure")

    callback = fail if point == "archive" else Mock()
    if point == "after-settings":

        def arm_flush_failure(session: Session) -> None:
            current = session.get(Event, EVENT_ID)
            configuration = require_event_configuration(session, current)

            def fail_after_reset_settings(
                _session: Session, _flush_context: object, _instances: object
            ) -> None:
                if current.state is EventState.LOBBY_OPEN:
                    assert (
                        configuration.record.settings_json == get_quiz_settings().model_dump_json()
                    )
                    raise HostRefusedError("test_failure")

            event.listen(session, "before_flush", fail_after_reset_settings)

        callback = arm_flush_failure
    if point == "after-delete":
        monkeypatch.setattr(
            reset_data,
            "initial_event_configuration",
            Mock(side_effect=HostRefusedError("test_failure")),
        )
    with pytest.raises(HostRefusedError, match="test_failure"):
        _reset(database, callback)
    with Session(database.engine) as session:
        assert session.get(Event, EVENT_ID).version == 12
        configuration = require_event_configuration(session, session.get(Event, EVENT_ID))
        assert configuration.final_round_id == ROUND_ID
        assert configuration.award_ceremony_phase == award_phase
        assert session.get(QuizRound, ROUND_ID) is not None
        assert session.get(Participant, PARTICIPANT_ID) is not None
        assert session.scalar(select(func.count()).select_from(Answer)) == 1
        assert session.scalar(select(OperationAudit)).operation == "complete_event"


@pytest.mark.parametrize("mismatch", ["id", "version", "multiple"])
def test_target_mismatch_refuses_before_archive(database: Database, mismatch: str) -> None:
    if mismatch == "multiple":
        with Session(database.engine) as session, session.begin():
            original = session.get(Event, EVENT_ID)
            session.add(
                Event(
                    id=uuid4(),
                    slug="another",
                    title="another",
                    scoring_policy_version=original.scoring_policy_version,
                    display_name_policy_version=original.display_name_policy_version,
                )
            )
    archive = Mock()
    with pytest.raises(HostRefusedError, match="mismatch"):
        _reset(
            database,
            archive,
            version=11 if mismatch == "version" else 12,
            event_id=uuid4() if mismatch == "id" else EVENT_ID,
        )
    archive.assert_not_called()


def test_stored_configuration_mismatch_refuses_before_archive(database: Database) -> None:
    with Session(database.engine) as session, session.begin():
        session.get(EventQuizConfiguration, EVENT_ID).settings_json = "{}"
    archive = Mock()
    with pytest.raises(DomainInvariantError, match="event_quiz_configuration_invalid"):
        _reset(database, archive)
    archive.assert_not_called()
    with Session(database.engine) as session:
        assert session.get(Participant, PARTICIPANT_ID) is not None


def test_committed_reset_cannot_be_repeated_with_original_expected_version(
    database: Database,
) -> None:
    _reset(database, Mock())
    archive = Mock()
    with pytest.raises(HostRefusedError, match="review_reset_event_version_mismatch"):
        _reset(database, archive)
    archive.assert_not_called()


@pytest.mark.parametrize(
    "last_value,is_called,remaining",
    [(1, False, 3), (1, True, 2), (3, True, None), (4, False, None), (2, False, None)],
)
def test_sequence_is_only_read_and_never_rewound(
    last_value: int, is_called: bool, remaining: int | None
) -> None:
    session = MagicMock()
    session.get.return_value = ParticipantIdPool(event_id=EVENT_ID, capacity=3)
    session.scalar.return_value = 3
    execute = session.connection.return_value.exec_driver_sql
    execute.return_value.mappings.return_value.one.return_value = row_mapping(
        {
            "last_value": last_value,
            "is_called": is_called,
        }
    )
    if remaining is None:
        with pytest.raises(HostRefusedError):
            reset_data._require_available_participant_ids(session, EVENT_ID)
    else:
        assert reset_data._require_available_participant_ids(session, EVENT_ID) == remaining
    statement = execute.call_args.args[0]
    assert statement.startswith("SELECT last_value, is_called FROM public.")
    assert statement.endswith(f'"participant_id_{EVENT_ID.hex}"')
    assert execute.call_count == 1


@pytest.mark.parametrize(
    "active_application,wrong_identity", [(False, False), (True, False), (False, True)]
)
def test_database_guard_checks_identity_lock_and_active_connections(
    monkeypatch: pytest.MonkeyPatch, active_application: bool, wrong_identity: bool
) -> None:
    session = MagicMock()
    session.execute.return_value.mappings.return_value.one.side_effect = [
        row_mapping(
            {
                "database_name": "other" if wrong_identity else "quiz",
                "current_role": "quizadmin",
                "session_role": "quizadmin",
            }
        ),
        row_mapping({"present": active_application}),
    ]
    schema, operator = Mock(), Mock()
    monkeypatch.setattr(reset_data, "_require_known_schema", Mock())
    monkeypatch.setattr(reset_data, "_require_current_schema", schema)
    monkeypatch.setattr(reset_data, "_require_operator", operator)
    if active_application or wrong_identity:
        with pytest.raises((HostRefusedError, ValidationError)):
            reset_data._require_database_ready(session)
        schema.assert_not_called()
        operator.assert_not_called()
    else:
        reset_data._require_database_ready(session)
        schema.assert_called_once_with(session)
        operator.assert_called_once_with(session)
        statements = [str(call.args[0]) for call in session.execute.call_args_list]
        assert statements[:2] == [
            "SET LOCAL lock_timeout = '5s'",
            "SET LOCAL statement_timeout = '30s'",
        ]
        assert "pg_advisory_xact_lock" in statements[3]
        assert "pg_stat_activity" in statements[4]


@pytest.mark.parametrize("difference", ["none", "schema", "table", "missing-table"])
def test_unknown_schema_or_unarchived_tables_refuse(
    monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    session = MagicMock()
    session.execute.return_value.mappings.return_value.one.return_value = row_mapping(
        {"present": difference == "schema"}
    )
    tables = {"alembic_version", *SQLModel.metadata.tables}
    if difference == "table":
        tables.add("unarchived_data")
    if difference == "missing-table":
        tables.remove("events")
    inspector = Mock(get_table_names=Mock(return_value=sorted(tables)))
    monkeypatch.setattr(reset_data, "inspect", Mock(return_value=inspector))
    if difference == "none":
        reset_data._require_known_schema(session)
    else:
        with pytest.raises(HostRefusedError, match="review_reset_unexpected"):
            reset_data._require_known_schema(session)
    if difference == "schema":
        inspector.get_table_names.assert_not_called()
    else:
        inspector.get_table_names.assert_called_once_with(schema="public")


def test_database_time_accepts_sqlalchemy_row_mapping() -> None:
    session = MagicMock()
    session.execute.return_value.mappings.return_value.one.return_value = row_mapping({"now": NOW})
    assert reset_data._database_time(session) == NOW


def test_inspection_uses_app_read_only_transaction_before_reset(
    inputs: HostInput, secrets_fixture: HostSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings(inputs, secrets_fixture, EVENT_ID)
    session, engine = MagicMock(), MagicMock()
    session.__enter__.return_value = session
    session.scalars.return_value.all.return_value = [Event(id=EVENT_ID, version=12)]
    create = Mock(return_value=engine)
    monkeypatch.setattr(reset_data, "_create_reset_engine", create)
    monkeypatch.setattr(reset_data, "Session", Mock(return_value=session))
    monkeypatch.setattr(reset_data, "_require_known_schema", Mock())
    monkeypatch.setattr(reset_data, "_require_current_schema", Mock())
    monkeypatch.setattr(reset_data, "require_event_configuration", Mock())
    reset_data.inspect_reset_event(settings, EVENT_ID, 12)
    assert create.call_args.args[0] == settings.database_url
    assert str(session.execute.call_args.args[0]) == "SET TRANSACTION READ ONLY"
    engine.dispose.assert_called_once()


def test_driver_failure_rolls_back_without_exposing_sensitive_details(
    inputs: HostInput, secrets_fixture: HostSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings: Settings = runtime_settings(inputs, secrets_fixture, EVENT_ID)
    session, engine = MagicMock(), MagicMock()
    session.__enter__.return_value = session
    transaction = session.begin.return_value
    monkeypatch.setattr(reset_data, "_create_reset_engine", Mock(return_value=engine))
    monkeypatch.setattr(reset_data, "Session", Mock(return_value=session))
    monkeypatch.setattr(
        reset_data,
        "_reset_in_transaction",
        Mock(side_effect=SQLAlchemyError("private-database-password")),
    )
    with pytest.raises(HostRefusedError, match="review_reset_database_failed") as captured:
        reset_data.reset_event_data(
            settings, secrets_fixture, EVENT_ID, 12, reset_id=RESET_ID, before_delete=Mock()
        )
    assert "private-database-password" not in str(captured.value)
    assert captured.value.__suppress_context__
    assert transaction.__exit__.call_args.args[0] is SQLAlchemyError
    engine.dispose.assert_called_once()
