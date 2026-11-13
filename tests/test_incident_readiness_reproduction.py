"""86人障害の旧対象条件に対するP0回帰。PostgreSQLのlock待ちは測定しない。"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import ClauseElement
from sqlmodel import Session, SQLModel

from quiz_app.domain.errors import InvalidStateTransitionError, VersionConflictError
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import (
    Event,
    Participant,
    ParticipantSession,
    QuizRound,
    ReadinessReport,
    RoundPreparationTarget,
)
from quiz_app.models.enums import RoundState
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)

# SQLiteではtimezoneを保持しないため、全fixture時刻をUTCのnaive値に統一する。
NOW = datetime(2026, 9, 30, 1, 59, 9)
EVENT_ID, ROUND_ID, QUESTION_ID = (UUID(int=value) for value in (860_001, 860_002, 860_003))
PARTICIPANT_IDS = tuple(UUID(int=861_000 + index) for index in range(86))


@dataclass(frozen=True)
class IncidentFixture:
    engine: Engine
    domain: QuizDomainService


def _add_participant(session: Session, index: int, last_seen_at: datetime) -> None:
    session.add(
        Participant(
            id=PARTICIPANT_IDS[index],
            event_id=EVENT_ID,
            public_label=f"A{chr(65 + index // 26)}{chr(65 + index % 26)}",
        )
    )
    session.add(
        ParticipantSession(
            id=UUID(int=862_000 + index),
            participant_id=PARTICIPANT_IDS[index],
            token_hash=f"fixture-{index}",
            last_seen_at=last_seen_at,
            expires_at=NOW + timedelta(hours=1),
        )
    )


@pytest.fixture
def incident(monkeypatch: pytest.MonkeyPatch) -> Iterator[IncidentFixture]:
    # DB URL設定を参照せず、新規のin-memory DBだけへ架空dataを投入する。
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def register_clock(connection: object, _: object) -> None:
        assert isinstance(connection, sqlite3.Connection)
        connection.create_function("clock_timestamp", 0, lambda: NOW.isoformat(" "))

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            Event(
                id=EVENT_ID,
                slug="incident-reproduction",
                title="[DEMO] 86人の準備対象検証",
                scoring_policy_version=get_quiz_settings().scoring.version,
                display_name_policy_version="fixture",
            )
        )
        session.add(configured_scoring_policy(get_quiz_settings()))
        session.add(initial_event_configuration(EVENT_ID, get_quiz_settings()))
        session.add(QuizRound(id=ROUND_ID, event_id=EVENT_ID, question_id=QUESTION_ID, version=1))
        for index in range(84):
            # prepare時点は84人。45人はちょうど5秒前、残り39人は5秒と1μs前。
            age = timedelta(seconds=5, microseconds=int(index >= 45))
            _add_participant(session, index, NOW - age)
    domain = QuizDomainService(engine)
    monkeypatch.setattr(domain, "_database_time", lambda _: NOW)
    yield IncidentFixture(engine, domain)
    engine.dispose()


def _capture_postgres_sql(engine: Engine, statements: list[str]) -> None:
    @event.listens_for(engine, "before_execute")
    def capture(
        _connection: Connection,
        statement: ClauseElement,
        _multiparams: object,
        _params: object,
        _options: object,
    ) -> None:
        statements.append(str(statement.compile(dialect=postgresql.dialect())))


def test_preparation_allows_schedule_without_any_reports_or_activity_selection(
    incident: IncidentFixture,
) -> None:
    statements: list[str] = []
    _capture_postgres_sql(incident.engine, statements)
    prepared = incident.domain.begin_preparation(ROUND_ID, expected_version=1)
    assert prepared.round_state is RoundState.READY_TO_SCHEDULE
    assert prepared.round_version == 2
    # 84人のうち39人は旧5秒窓の外、全員からACKがなくても運営は予約できる。
    assert not any(
        "participant_sessions" in statement
        or "readiness_reports" in statement
        or "round_preparation_targets" in statement
        for statement in statements
    )
    with Session(incident.engine) as session, session.begin():
        for index in (84, 85):
            _add_participant(session, index, NOW)
    scheduled = incident.domain.schedule_round(ROUND_ID, expected_version=2)
    assert scheduled.open_at == NOW + timedelta(seconds=3)
    assert scheduled.standard_close_at == NOW + timedelta(seconds=13)
    with Session(incident.engine) as session:
        assert session.scalar(select(func.count()).select_from(Participant)) == 86
        assert session.scalar(select(func.count()).select_from(ReadinessReport)) == 0
        assert session.scalar(select(func.count()).select_from(RoundPreparationTarget)) == 0


def test_prepare_keeps_version_conflicts_and_rejects_duplicate_action(
    incident: IncidentFixture,
) -> None:
    incident.domain.begin_preparation(ROUND_ID, expected_version=1)
    with pytest.raises(VersionConflictError):
        incident.domain.begin_preparation(ROUND_ID, expected_version=1)
    with pytest.raises(InvalidStateTransitionError, match="round_is_not_preparing_assets"):
        incident.domain.begin_preparation(ROUND_ID, expected_version=2)


def test_empty_event_can_be_prepared_and_scheduled(incident: IncidentFixture) -> None:
    # 対象者数を開始制限へ戻さない。旧rowsを消さず別の無参加開催をfixtureへ追加する。
    empty_event_id, empty_round_id = UUID(int=90001), UUID(int=90002)
    with Session(incident.engine) as session, session.begin():
        session.add(
            Event(
                id=empty_event_id,
                slug="empty",
                title="[DEMO] empty",
                scoring_policy_version=get_quiz_settings().scoring.version,
                display_name_policy_version="fixture",
            )
        )
        session.add(initial_event_configuration(empty_event_id, get_quiz_settings()))
        session.add(
            QuizRound(
                id=empty_round_id, event_id=empty_event_id, question_id=QUESTION_ID, version=1
            )
        )
    incident.domain.begin_preparation(empty_round_id, expected_version=1)
    scheduled = incident.domain.schedule_round(empty_round_id, expected_version=2)
    assert scheduled.version == 3


def test_scheduling_does_not_update_legacy_readiness_version(incident: IncidentFixture) -> None:
    with Session(incident.engine) as session, session.begin():
        session.add(
            ReadinessReport(
                participant_id=PARTICIPANT_IDS[0],
                round_id=ROUND_ID,
                round_version=1,
                is_ready=True,
                has_asset_failure=False,
                reported_at=NOW,
                last_seen_at=NOW,
            )
        )
    incident.domain.begin_preparation(ROUND_ID, expected_version=1)
    incident.domain.schedule_round(ROUND_ID, expected_version=2)
    with Session(incident.engine) as session:
        report = session.scalar(select(ReadinessReport))
        assert report is not None
        assert report.round_version == 1
        assert report.last_seen_at == NOW
