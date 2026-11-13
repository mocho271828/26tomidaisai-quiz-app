import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, select
from sqlmodel import Session, SQLModel

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import ParticipantOutcomeResponse, PersonalResultRevision
from quiz_app.audience.result_batch import ResultBatchKey, load_result_batch
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import (
    Answer,
    Event,
    OperationAudit,
    Participant,
    ParticipantSession,
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    RoundPreparationTarget,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    EventState,
    RoundState,
    ScoreExclusionReason,
    SpeedCohort,
)
from quiz_app.quiz_configuration import ResultVisibility

pytestmark = pytest.mark.usefixtures("legacy_quiz_configuration")
EVENT_ID = UUID(int=840_001)
NOW = datetime(2026, 9, 28, 10)


@pytest.fixture
def scoring_session(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Session, QuizDomainService]]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def register_clock(database_connection: object, _: object) -> None:
        assert isinstance(database_connection, sqlite3.Connection)
        database_connection.create_function("clock_timestamp", 0, lambda: NOW.isoformat(" "))

    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        # SQLiteではPostgreSQL専用partial条件が消えるため、fixture内だけindexを除く。
        connection.exec_driver_sql("DROP INDEX uq_question_options_one_correct")
        connection.exec_driver_sql("DROP INDEX uq_quiz_rounds_one_active_per_event")
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                Event(
                    id=EVENT_ID,
                    slug="practice-scoring",
                    title="練習問題の採点",
                    state=EventState.IN_PROGRESS,
                    scoring_policy_version="practice-test-v1",
                    display_name_policy_version="name-v1",
                ),
                ScoringPolicy(
                    version="practice-test-v1",
                    base_points=400,
                    initial_speed_bonus=100,
                    speed_bucket_ms=100,
                    speed_decrement_per_bucket=1,
                    standard_duration_ms=10_000,
                    is_approved=True,
                ),
            ]
        )
        session.commit()
        domain = QuizDomainService(engine)
        monkeypatch.setattr(domain, "_database_time", lambda _: NOW)
        yield session, domain
    engine.dispose()


def _participant(session: Session, label: str) -> Participant:
    participant = Participant(event_id=EVENT_ID, public_label=label)
    session.add(participant)
    return participant


def _publish(
    session: Session,
    domain: QuizDomainService,
    number: int,
    answers: list[tuple[Participant, int, bool]],
) -> QuizRound:
    question = Question(
        event_id=EVENT_ID,
        version=number,
        prompt=f"問題{number}",
        display_order=10 - number,
    )
    correct = QuestionOption(
        question_id=question.id, label="A", content="正解", display_order=0, is_correct=True
    )
    wrong = QuestionOption(
        question_id=question.id, label="B", content="不正解", display_order=1, is_correct=False
    )
    open_at = NOW - timedelta(seconds=15)
    round_ = QuizRound(
        event_id=EVENT_ID,
        question_id=question.id,
        version=number * 10,
        state=RoundState.AWAITING_VALIDATION,
        open_at=open_at,
        standard_close_at=NOW - timedelta(seconds=5),
    )
    session.add_all([question, correct, wrong, round_])
    session.add_all(
        Answer(
            participant_id=participant.id,
            round_id=round_.id,
            option_id=correct.id if is_correct else wrong.id,
            client_answer_id=uuid4(),
            received_at=open_at + timedelta(milliseconds=elapsed_ms),
        )
        for participant, elapsed_ms, is_correct in answers
    )
    session.flush()
    domain.aggregate_round_results_in_transaction(session, round_.id, round_.version)
    domain.publish_round_results_in_transaction(session, round_.id, round_.version)
    session.commit()
    return round_


def _ranking(session: Session, round_: QuizRound) -> dict[str, tuple[int, int, int]]:
    rows = session.scalars(
        select(RankSnapshotRow)
        .join(RankSnapshot, RankSnapshot.id == RankSnapshotRow.snapshot_id)
        .where(RankSnapshot.round_id == round_.id)
    ).all()
    return {
        row.public_label: (row.overall_rank, row.total_points, row.correct_count) for row in rows
    }


def _personal_outcome(
    session: Session, participant: Participant, round_: QuizRound
) -> ParticipantOutcomeResponse:
    revision = PersonalResultRevision(
        event_id=EVENT_ID,
        event_version=1,
        result_round_id=round_.id,
        result_round_version=round_.version,
        ranking_snapshot_version=None,
        result_visibility=ResultVisibility(
            distribution=False, personal_ranking=True, overall_ranking=False
        ),
    )
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    batch = load_result_batch(engine, ResultBatchKey.from_revision(revision))
    principal = ParticipantPrincipal(
        participant.id, UUID(int=1), EVENT_ID, participant.public_label, True
    )
    outcome = batch.for_participant(principal, revision).outcome
    assert outcome is not None
    return outcome


def test_one_practice_keeps_outcome_and_eight_official_rounds_rank_from_zero(
    scoring_session: tuple[Session, QuizDomainService],
) -> None:
    session, domain = scoring_session
    regular = _participant(session, "AAA")
    practice_only = _participant(session, "CCC")
    wrong_only = _participant(session, "EEE")
    _participant(session, "FFF")
    practice = _publish(
        session,
        domain,
        1,
        [
            (regular, 100, True),
            (practice_only, 9000, True),
            (wrong_only, 900, False),
        ],
    )

    assert _ranking(session, practice) == {"AAA": (1, 0, 0), "CCC": (1, 0, 0), "EEE": (1, 0, 0)}
    outcome = _personal_outcome(session, regular, practice)
    assert outcome.status == "Scored"
    assert outcome.total_points == 499
    assert outcome.speed_rank == 1

    late_joiner = _participant(session, "BBB")
    # 旧データの参加資格値に依存せず、途中参加者を採点・総合順位へ含める。
    late_joiner.is_ranking_eligible = False
    for number in range(2, 10):
        round_ = _publish(
            session,
            domain,
            number,
            [(regular, 1000, True), (late_joiner, 1000, True)],
        )
        assert _ranking(session, round_) == {
            "AAA": (1, 490 * (number - 1), number - 1),
            "BBB": (1, 490 * (number - 1), number - 1),
            "CCC": (3, 0, 0),
            "EEE": (3, 0, 0),
        }
        ledger = session.scalar(
            select(ScoreLedger).where(
                ScoreLedger.round_id == round_.id,
                ScoreLedger.participant_id == late_joiner.id,
            )
        )
        assert ledger is not None
        assert ledger.speed_rank == 1
        assert ledger.speed_cohort is SpeedCohort.CORRECT
        assert ledger.exclusion_reason is ScoreExclusionReason.NONE

    outcome = _personal_outcome(session, regular, practice)
    assert outcome.status == "Scored"
    assert outcome.total_points == 499


@pytest.mark.parametrize("void_state", [RoundState.VOIDED, RoundState.ROUND_BACKUP_VALIDATED])
@pytest.mark.parametrize("void_offset,expected_total", [(-1, 0), (0, 500), (1, 500)])
def test_practice_classification_uses_started_round_history_after_void(
    scoring_session: tuple[Session, QuizDomainService],
    void_state: RoundState,
    void_offset: int,
    expected_total: int,
) -> None:
    session, domain = scoring_session
    question = Question(event_id=EVENT_ID, version=100, prompt="中止問題", display_order=1)
    voided = QuizRound(
        event_id=EVENT_ID,
        question_id=question.id,
        version=10,
        state=void_state,
        open_at=NOW - timedelta(minutes=1),
    )
    session.add_all(
        [
            question,
            voided,
            OperationAudit(
                event_id=EVENT_ID,
                round_id=voided.id,
                operation="void_round",
                target_type="quiz_round",
                target_id=str(voided.id),
                before_state=RoundState.SCHEDULED.value,
                after_state=RoundState.VOIDED.value,
                reason="VOIDの出題番号を検証",
                outcome="succeeded",
                occurred_at=voided.open_at + timedelta(seconds=void_offset),
            ),
        ]
    )
    participant = _participant(session, "AAA")
    round_ = _publish(session, domain, 2, [(participant, 0, True)])

    assert _ranking(session, round_) == {"AAA": (1, expected_total, int(expected_total > 0))}


def test_unanswered_practice_does_not_make_first_official_round_practice(
    scoring_session: tuple[Session, QuizDomainService],
) -> None:
    session, domain = scoring_session
    practice = _publish(session, domain, 1, [])
    assert _ranking(session, practice) == {}
    participant = _participant(session, "AAA")
    scored = _publish(session, domain, 2, [(participant, 0, True)])
    assert _ranking(session, scored) == {"AAA": (1, 500, 1)}


def test_former_late_joiner_does_not_require_preparation_target(
    scoring_session: tuple[Session, QuizDomainService],
) -> None:
    session, domain = scoring_session
    participant = _participant(session, "AAA")
    participant.is_ranking_eligible = False
    question = Question(event_id=EVENT_ID, version=1, prompt="準備中", display_order=1)
    round_ = QuizRound(event_id=EVENT_ID, question_id=question.id, version=1)
    session.add_all(
        [
            question,
            round_,
            ParticipantSession(
                participant_id=participant.id,
                token_hash=uuid4().hex,
                expires_at=NOW + timedelta(hours=1),
                last_seen_at=NOW,
            ),
        ]
    )
    session.flush()

    preparation = domain.begin_preparation_in_transaction(session, round_.id, round_.version)

    assert preparation.round_state is RoundState.READY_TO_SCHEDULE
    assert preparation.round_version == 2
    assert session.scalar(select(RoundPreparationTarget)) is None
