import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.round_number import quiz_round_number
from quiz_app.models import OperationAudit, Question, QuizRound
from quiz_app.models.enums import RoundState

EVENT_ID = UUID(int=23)
OTHER_EVENT_ID = UUID(int=24)
OPEN_AT = datetime(2026, 9, 23, 10, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        # SQLiteではPostgreSQL専用の部分条件が消えるため、履歴Round用の制約を除く。
        connection.exec_driver_sql("DROP INDEX uq_quiz_rounds_one_active_per_event")
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def _round(
    session: Session,
    version: int,
    *,
    event_id: UUID = EVENT_ID,
    state: RoundState = RoundState.ROUND_BACKUP_VALIDATED,
    open_at: datetime | None = None,
    display_order: int = 1,
) -> QuizRound:
    question = Question(
        event_id=event_id,
        version=version,
        display_order=display_order,
        prompt=f"問題 {version}",
        created_at=OPEN_AT,
    )
    round_ = QuizRound(
        event_id=event_id,
        question_id=question.id,
        version=version,
        state=state,
        open_at=open_at,
        created_at=OPEN_AT,
    )
    session.add_all([question, round_])
    return round_


def _void_audit(round_: QuizRound, occurred_at: datetime) -> OperationAudit:
    return OperationAudit(
        event_id=round_.event_id,
        round_id=round_.id,
        operation="void_round",
        target_type="quiz_round",
        target_id=str(round_.id),
        before_state=RoundState.SCHEDULED.value,
        after_state=RoundState.VOIDED.value,
        reason="テスト用VOID確定",
        outcome="succeeded",
        occurred_at=occurred_at,
    )


def test_number_follows_round_history_independently_of_question_order(session: Session) -> None:
    rounds = [
        _round(session, version, display_order=order)
        for version, order in [(7, 40), (23, 2), (56, 91), (74, 5)]
    ]
    _round(session, 90)
    _round(session, 2, event_id=OTHER_EVENT_ID)
    _round(session, 9, event_id=OTHER_EVENT_ID)
    session.commit()

    assert [quiz_round_number(session, round_) for round_ in rounds] == [1, 2, 3, 4]

    selected_question = Question(
        event_id=EVENT_ID,
        version=200,
        display_order=1,
        prompt="準備中に選択し直した問題",
        created_at=OPEN_AT,
    )
    session.add(selected_question)
    rounds[3].question_id = selected_question.id
    rounds[3].version = 80
    session.commit()

    assert quiz_round_number(session, rounds[3]) == 4


def test_stale_round_snapshot_keeps_number_after_persisted_version_advances(
    session: Session,
) -> None:
    _round(session, 9)
    current = _round(session, 74)
    session.commit()
    stale_snapshot = QuizRound(
        id=current.id,
        event_id=current.event_id,
        question_id=current.question_id,
        version=current.version,
    )

    current.version = 81
    session.commit()

    assert quiz_round_number(session, stale_snapshot) == 2
    assert quiz_round_number(session, current) == 2


@pytest.mark.parametrize("state", [RoundState.VOIDED, RoundState.ROUND_BACKUP_VALIDATED])
def test_unscheduled_void_does_not_consume_a_number(session: Session, state: RoundState) -> None:
    void_round = _round(session, 7, state=state)
    current = _round(session, 74)
    if state is RoundState.ROUND_BACKUP_VALIDATED:
        session.add(_void_audit(void_round, OPEN_AT))
    session.commit()

    assert quiz_round_number(session, current) == 1


@pytest.mark.parametrize("state", [RoundState.VOIDED, RoundState.ROUND_BACKUP_VALIDATED])
@pytest.mark.parametrize("open_year", [2000, 2100])
@pytest.mark.parametrize(("void_offset", "number"), [(-1, 1), (0, 2), (1, 2)])
def test_void_number_uses_fixed_void_time_even_after_backup_or_scheduled_time_passes(
    session: Session,
    state: RoundState,
    open_year: int,
    void_offset: int,
    number: int,
) -> None:
    open_at = OPEN_AT.replace(year=open_year)
    void_round = _round(session, 7, state=state, open_at=open_at)
    current = _round(session, 74)
    session.add(_void_audit(void_round, open_at + timedelta(seconds=void_offset)))
    session.commit()

    assert quiz_round_number(session, current) == number
    session.expire_all()
    assert quiz_round_number(session, current) == number


@pytest.mark.parametrize(
    "invalid_audit",
    ["missing", "other_event", "failed", "other_target_type", "other_target_id", "conflicting"],
)
def test_scheduled_void_rejects_missing_or_inconsistent_timing(
    session: Session, invalid_audit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged_errors: list[DomainInvariantError] = []

    def record_exception(message: str) -> None:
        assert message == "round_number_projection_failed"
        error = sys.exception()
        assert isinstance(error, DomainInvariantError)
        assert error.__traceback__ is not None
        logged_errors.append(error)

    monkeypatch.setattr("quiz_app.domain.round_number.logfire.exception", record_exception)
    void_round = _round(session, 7, state=RoundState.VOIDED, open_at=OPEN_AT)
    current = _round(session, 74)
    audit = _void_audit(void_round, OPEN_AT + timedelta(seconds=1))
    if invalid_audit == "other_event":
        audit.event_id = OTHER_EVENT_ID
    elif invalid_audit == "failed":
        audit.outcome = "failed"
    elif invalid_audit == "other_target_type":
        audit.target_type = "event"
    elif invalid_audit == "other_target_id":
        audit.target_id = str(UUID(int=100))
    elif invalid_audit == "conflicting":
        session.add(_void_audit(void_round, OPEN_AT - timedelta(seconds=1)))
    if invalid_audit != "missing":
        session.add(audit)
    session.commit()

    with pytest.raises(
        DomainInvariantError, match="round_number_void_timing_unavailable"
    ) as raised:
        quiz_round_number(session, current)

    assert logged_errors == [raised.value]


def test_duplicate_identical_void_audits_do_not_count_the_round_twice(session: Session) -> None:
    void_round = _round(session, 7, state=RoundState.VOIDED, open_at=OPEN_AT)
    current = _round(session, 74)
    session.add_all([_void_audit(void_round, OPEN_AT), _void_audit(void_round, OPEN_AT)])
    session.commit()

    assert quiz_round_number(session, current) == 2
