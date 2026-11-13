import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine, select
from sqlalchemy import event as sqlalchemy_event
from sqlmodel import Session, SQLModel

import quiz_app.operator.mutations as mutations_module
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import (
    Answer,
    Event,
    IdempotencyRecord,
    OperationAudit,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    ReadinessReport,
    RoundOptionStat,
    RoundPreparationTarget,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RankingVisibility,
    RoundResultPublication,
    RoundState,
    SpeedCohort,
)
from quiz_app.operator.auth_service import OperatorPrincipal
from quiz_app.operator.control_contracts import (
    NextRoundMutationRequest,
    OperatorMutationRequest,
)
from quiz_app.operator.mutations import OperatorMutationService
from quiz_app.round_backup import RoundBackupService

pytestmark = pytest.mark.usefixtures("legacy_quiz_configuration")

EVENT_ID = UUID("74000000-0000-4000-8000-000000000001")
INITIAL_ROUND_ID = UUID("74000000-0000-4000-8000-000000000002")
ACCOUNT_ID = UUID("74000000-0000-4000-8000-000000000003")
SESSION_ID = UUID("74000000-0000-4000-8000-000000000004")
PARTICIPANT_ID = UUID("74000000-0000-4000-8000-000000000005")
QUESTION_IDS = tuple(UUID(int=20_000 + index) for index in range(8))
NOW = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)


class AdvisoryIgnoringSession(Session):
    """SQLiteに存在しないPostgreSQL advisory lockだけをtest境界で吸収する。"""

    def execute(self, statement: object, *args: object, **kwargs: object) -> object:
        if "pg_advisory_xact_lock" in str(statement):
            return None
        return super().execute(statement, *args, **kwargs)


class SqliteMutationSessionFactory:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, _: Engine) -> AdvisoryIgnoringSession:
        return AdvisoryIgnoringSession(self._engine, expire_on_commit=False)


@dataclass(frozen=True, slots=True)
class ProgressionFixture:
    engine: Engine
    service: OperatorMutationService
    backup_directory: Path


def _option_ids(question_position: int) -> tuple[UUID, ...]:
    return tuple(UUID(int=30_000 + question_position * 10 + index) for index in range(4))


def _principal() -> OperatorPrincipal:
    return OperatorPrincipal(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        username="operator",
        role=AdminRole.OPERATOR,
        csrf_hash="csrf-hash",
    )


def _published_round_rows(round_: QuizRound, question_position: int) -> list[SQLModel]:
    option_ids = _option_ids(question_position)
    snapshot = RankSnapshot(
        event_id=EVENT_ID,
        round_id=round_.id,
        version=question_position + 1,
        generated_at=NOW,
    )
    rows: list[SQLModel] = [
        Answer(
            participant_id=PARTICIPANT_ID,
            round_id=round_.id,
            option_id=option_ids[1],
            client_answer_id=UUID(int=40_000 + question_position),
            received_at=NOW - timedelta(seconds=11),
        ),
        ScoreLedger(
            round_id=round_.id,
            participant_id=PARTICIPANT_ID,
            scoring_policy_version="score-v1",
            is_correct=True,
            base_points=100,
            speed_points=20,
            total_points=120,
            elapsed_ms=1_000,
            speed_bucket=1,
            speed_rank=1,
            speed_cohort_size=1,
            speed_cohort=SpeedCohort.CORRECT,
        ),
        ReadinessReport(
            participant_id=PARTICIPANT_ID,
            round_id=round_.id,
            round_version=round_.version,
            is_ready=True,
            reported_at=NOW,
            last_seen_at=NOW,
        ),
        RoundPreparationTarget(
            round_id=round_.id,
            participant_id=PARTICIPANT_ID,
            captured_at=NOW,
        ),
        snapshot,
        RankSnapshotRow(
            snapshot_id=snapshot.id,
            public_label="AAA",
            overall_rank=1,
            total_points=120 * max(0, question_position - 1),
            correct_count=max(0, question_position - 1),
        ),
    ]
    rows.extend(
        RoundOptionStat(
            round_id=round_.id,
            option_id=option_id,
            valid_response_count=1 if index == 1 else 0,
            response_percentage=Decimal("100.00") if index == 1 else Decimal("0.00"),
        )
        for index, option_id in enumerate(option_ids)
    )
    return rows


def _mark_round_published(session: Session, round_: QuizRound, position: int) -> None:
    round_.state = RoundState.REVEAL
    round_.result_publication = RoundResultPublication.PUBLISHED
    round_.revealed_at = NOW
    round_.open_at = NOW - timedelta(seconds=12)
    round_.standard_close_at = NOW - timedelta(seconds=2)
    session.add_all(_published_round_rows(round_, position))
    session.commit()


def _assert_new_round_has_no_carried_state(session: Session, round_id: UUID) -> None:
    assert session.scalars(select(Answer).where(Answer.round_id == round_id)).all() == []
    assert session.scalars(select(ScoreLedger).where(ScoreLedger.round_id == round_id)).all() == []
    assert (
        session.scalars(select(ReadinessReport).where(ReadinessReport.round_id == round_id)).all()
        == []
    )
    assert (
        session.scalars(
            select(RoundPreparationTarget).where(RoundPreparationTarget.round_id == round_id)
        ).all()
        == []
    )


@pytest.fixture
def progression_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProgressionFixture]:
    engine = create_engine("sqlite://")

    @sqlalchemy_event.listens_for(engine, "connect")
    def register_clock(database_connection: object, _: object) -> None:
        assert isinstance(database_connection, sqlite3.Connection)
        database_connection.create_function(
            "clock_timestamp",
            0,
            lambda: NOW.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f"),
        )

    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_question_options_one_correct")
        connection.exec_driver_sql("DROP INDEX uq_quiz_rounds_one_active_per_event")

    event = Event(
        id=EVENT_ID,
        slug="variable-round-progression",
        title="[DEMO] variable round progression",
        state=EventState.IN_PROGRESS,
        ranking_visibility=RankingVisibility.HIDDEN_ALL,
        scoring_policy_version="score-v1",
        display_name_policy_version="name-v1",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    questions = [
        Question(
            id=question_id,
            event_id=EVENT_ID,
            version=position + 1,
            prompt=f"[DEMO] 問題{position + 1}",
            display_order=position + 1,
            created_at=NOW,
        )
        for position, question_id in enumerate(QUESTION_IDS)
    ]
    options = [
        QuestionOption(
            id=option_id,
            question_id=question.id,
            label=chr(ord("A") + option_position),
            content=f"選択肢{option_position + 1}",
            display_order=option_position,
            is_correct=option_position == 1,
        )
        for question_position, question in enumerate(questions)
        for option_position, option_id in enumerate(_option_ids(question_position))
    ]
    first_round = QuizRound(
        id=INITIAL_ROUND_ID,
        event_id=EVENT_ID,
        question_id=QUESTION_IDS[0],
        version=1,
        state=RoundState.REVEAL,
        result_publication=RoundResultPublication.PUBLISHED,
        revealed_at=NOW,
        open_at=NOW - timedelta(seconds=12),
        standard_close_at=NOW - timedelta(seconds=2),
        created_at=NOW,
    )
    with Session(engine, expire_on_commit=False) as session:
        session.add(
            ScoringPolicy(
                version="score-v1",
                base_points=100,
                initial_speed_bonus=21,
                speed_bucket_ms=1000,
                speed_decrement_per_bucket=1,
                standard_duration_ms=10_000,
                is_approved=True,
            )
        )
        session.add_all([event, *questions, *options, first_round])
        session.add_all(_published_round_rows(first_round, 0))
        session.commit()

    domain_service = QuizDomainService(engine)
    mutation_service = OperatorMutationService(
        engine,
        domain_service,
        RoundBackupService(tmp_path, Fernet.generate_key().decode("ascii")),
    )
    monkeypatch.setattr(mutations_module, "Session", SqliteMutationSessionFactory(engine))
    monkeypatch.setattr(domain_service, "_database_time", lambda _session: NOW)
    monkeypatch.setattr(mutation_service, "_database_time", lambda _session: NOW)
    monkeypatch.setattr(mutations_module.logfire, "info", lambda *_args, **_kwargs: None)
    yield ProgressionFixture(engine, mutation_service, tmp_path)
    engine.dispose()


@pytest.mark.parametrize("completed_round_count", [3, 7, 8])
def test_manual_completion_with_unused_questions_or_additional_official_round(
    progression_fixture: ProgressionFixture,
    completed_round_count: int,
) -> None:
    fixture = progression_fixture
    principal = _principal()
    current_round_id = INITIAL_ROUND_ID
    round_ids = [INITIAL_ROUND_ID]

    for position, next_question_id in enumerate(QUESTION_IDS[1:completed_round_count], start=1):
        with Session(fixture.engine) as session:
            current_round = session.get(QuizRound, current_round_id)
            assert current_round is not None
            expected_version = current_round.version
        response = fixture.service.advance_round(
            principal,
            current_round_id,
            NextRoundMutationRequest(
                expected_version=expected_version,
                idempotency_key=UUID(int=50_000 + position),
                question_id=next_question_id,
            ),
        )
        assert response.round_id is not None
        assert response.round_id not in round_ids
        current_round_id = response.round_id
        round_ids.append(current_round_id)

        with Session(fixture.engine, expire_on_commit=False) as session:
            new_round = session.get(QuizRound, current_round_id)
            assert new_round is not None
            assert new_round.question_id == next_question_id
            assert new_round.state is RoundState.PREPARING_ASSETS
            assert new_round.result_publication is RoundResultPublication.WITHHELD
            _assert_new_round_has_no_carried_state(session, current_round_id)
            all_rounds = session.scalars(
                select(QuizRound).where(QuizRound.event_id == EVENT_ID)
            ).all()
            active_rounds = [
                round_
                for round_ in all_rounds
                if round_.state not in {RoundState.VOIDED, RoundState.ROUND_BACKUP_VALIDATED}
            ]
            assert active_rounds == [new_round]
            _mark_round_published(session, new_round, position)
        assert len(list(fixture.backup_directory.glob("*.fernet"))) == position

    with Session(fixture.engine) as session:
        final_round = session.get(QuizRound, current_round_id)
        event = session.get(Event, EVENT_ID)
        assert final_round is not None
        assert event is not None
        assert event.state is EventState.IN_PROGRESS
        assert final_round.state is RoundState.REVEAL
        final_version = final_round.version
    completed = fixture.service.complete_event(
        principal,
        current_round_id,
        OperatorMutationRequest(
            expected_version=final_version,
            idempotency_key=UUID(int=50_000 + completed_round_count),
        ),
    )

    assert completed.operation == "complete_event"
    assert completed.round_id == current_round_id
    assert len(set(round_ids)) == completed_round_count
    assert len(list(fixture.backup_directory.glob("*.fernet"))) == completed_round_count
    with Session(fixture.engine) as session:
        event = session.get(Event, EVENT_ID)
        rounds = session.scalars(
            select(QuizRound).where(QuizRound.event_id == EVENT_ID).order_by(QuizRound.version)
        ).all()
        assert event is not None
        assert event.state is EventState.COMPLETED
        assert [round_.id for round_ in rounds] == round_ids
        assert [round_.question_id for round_ in rounds] == list(
            QUESTION_IDS[:completed_round_count]
        )
        assert len(session.scalars(select(Question)).all()) == len(QUESTION_IDS)
        assert all(round_.state is RoundState.ROUND_BACKUP_VALIDATED for round_ in rounds)
        assert {answer.round_id for answer in session.scalars(select(Answer)).all()} == set(
            round_ids
        )
        assert {ledger.round_id for ledger in session.scalars(select(ScoreLedger)).all()} == set(
            round_ids
        )
        assert {
            report.round_id for report in session.scalars(select(ReadinessReport)).all()
        } == set(round_ids)
        assert {
            target.round_id for target in session.scalars(select(RoundPreparationTarget)).all()
        } == set(round_ids)
        assert len(session.scalars(select(OperationAudit)).all()) == completed_round_count * 2
        assert len(session.scalars(select(IdempotencyRecord)).all()) == completed_round_count
