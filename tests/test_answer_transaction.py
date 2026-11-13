import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, event, func, select, update
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.domain.errors import (
    AnswerContentConflictError,
    AnswerWindowClosedError,
    DomainInvariantError,
    InvalidStateTransitionError,
    PublicationTooEarlyError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.domain.types import AcceptedAnswer, SubmitAnswerCommand
from quiz_app.models import (
    Answer,
    Event,
    Participant,
    Question,
    QuestionOption,
    QuizRound,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import EventState, RoundResultPublication, RoundState
from quiz_app.observability.answer_diagnostics import (
    AnswerDiagnostic,
    AnswerDiagnosticRecord,
    current_answer_diagnostic,
)
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
EVENT_ID = UUID(int=101)
ROUND_ID = UUID(int=102)
QUESTION_ID = UUID(int=103)
OPTION_ID = UUID(int=104)
PARTICIPANT_ID = UUID(int=105)
CLIENT_ANSWER_ID = UUID(int=106)


@pytest.fixture
def answer_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def clock(connection: object, _: object) -> None:
        assert isinstance(connection, sqlite3.Connection)
        connection.create_function(
            "clock_timestamp", 0, lambda: NOW.replace(tzinfo=None).isoformat(sep=" ")
        )

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        policy = configured_scoring_policy(get_quiz_settings())
        session.add(policy)
        session.add(initial_event_configuration(EVENT_ID, get_quiz_settings()))
        session.add(
            Event(
                id=EVENT_ID,
                slug="answer-core",
                title="test",
                state=EventState.IN_PROGRESS,
                scoring_policy_version=policy.version,
                display_name_policy_version="fixture",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            Question(
                id=QUESTION_ID,
                event_id=EVENT_ID,
                version=1,
                prompt="test",
                display_order=1,
                created_at=NOW,
            )
        )
        session.add(
            QuestionOption(
                id=OPTION_ID,
                question_id=QUESTION_ID,
                label="A",
                content="test",
                display_order=1,
                is_correct=True,
            )
        )
        session.add(Participant(id=PARTICIPANT_ID, event_id=EVENT_ID, public_label="abc"))
        session.add(
            QuizRound(
                id=ROUND_ID,
                event_id=EVENT_ID,
                question_id=QUESTION_ID,
                version=1,
                state=RoundState.OPEN,
                open_at=NOW - timedelta(seconds=1),
                standard_close_at=NOW + timedelta(seconds=9),
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def command(*, client_id: UUID = CLIENT_ANSWER_ID) -> SubmitAnswerCommand:
    return SubmitAnswerCommand(
        participant_id=PARTICIPANT_ID,
        round_id=ROUND_ID,
        round_version=1,
        option_id=OPTION_ID,
        client_answer_id=client_id,
        client_elapsed_ms=1000,
    )


def principal() -> ParticipantPrincipal:
    return ParticipantPrincipal(
        participant_id=PARTICIPANT_ID,
        session_id=UUID(int=107),
        event_id=EVENT_ID,
        public_label="abc",
        is_ranking_eligible=True,
    )


def test_submission_core_leaves_commit_and_rollback_to_caller(answer_engine: Engine) -> None:
    domain = QuizDomainService(answer_engine)
    projection = AudienceProjectionService(answer_engine)
    with Session(answer_engine) as session:
        receipt = domain.submit_answer_in_transaction(
            session, command(), expected_event_id=EVENT_ID
        )
        assert receipt.is_replay is False
        assert session.in_transaction()
        assert session.scalar(select(func.count()).select_from(Answer)) == 1
        session.rollback()
        assert (
            projection.get_answer_status_in_session(session, principal(), CLIENT_ANSWER_ID).status
            == "NotStored"
        )
    with Session(answer_engine) as session:
        participant = session.get(Participant, PARTICIPANT_ID)
        assert participant is not None and participant.first_answer_at is None


def test_sync_submission_commits_and_exact_replay_does_not_insert(answer_engine: Engine) -> None:
    service = QuizDomainService(answer_engine)
    receipt = service.submit_answer(command())
    replay = service.submit_answer(command())
    assert replay.answer_id == receipt.answer_id
    assert replay.is_replay is True
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == 1
        with pytest.raises(AnswerContentConflictError):
            service.get_answer_replay_in_session(session, command(client_id=UUID(int=999)))


def test_wrong_event_is_rejected_even_when_answer_is_already_stored(answer_engine: Engine) -> None:
    service = QuizDomainService(answer_engine)
    service.submit_answer(command())
    with (
        Session(answer_engine) as session,
        pytest.raises(ResourceNotFoundError, match="round_not_found"),
    ):
        service.submit_answer_in_transaction(session, command(), expected_event_id=UUID(int=999))


def test_database_deadline_failure_rolls_back_flushed_answer(answer_engine: Engine) -> None:
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        round_.open_at = NOW - timedelta(milliseconds=14_000)
        round_.standard_close_at = NOW - timedelta(milliseconds=4_000)
    with pytest.raises(AnswerWindowClosedError):
        QuizDomainService(answer_engine).submit_answer(command())
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == 0


def test_submission_uses_one_context_query_and_returned_database_stamp(
    answer_engine: Engine,
) -> None:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(answer_engine, "before_cursor_execute", capture)
    try:
        receipt = QuizDomainService(answer_engine).submit_answer(command())
    finally:
        event.remove(answer_engine, "before_cursor_execute", capture)
    assert receipt.received_at.replace(tzinfo=UTC) == NOW
    assert len(statements) == 4
    assert "LEFT OUTER JOIN participants" in statements[0]
    assert "LEFT OUTER JOIN question_options" in statements[0]
    assert "RETURNING received_at" in statements[2]
    with Session(answer_engine) as session:
        participant = session.get(Participant, PARTICIPANT_ID)
        assert participant is not None
        assert participant.first_answer_at == receipt.received_at


@pytest.mark.parametrize("missing", ["participant", "option"])
def test_missing_context_does_not_hide_prior_version_conflict(
    answer_engine: Engine, missing: str
) -> None:
    request = replace(
        command(),
        round_version=99,
        **{f"{missing}_id": UUID(int=999)},
    )
    with pytest.raises(VersionConflictError):
        QuizDomainService(answer_engine).submit_answer(request)
    request = replace(request, round_version=1)
    with pytest.raises(ResourceNotFoundError, match=f"{missing}_not_found"):
        QuizDomainService(answer_engine).submit_answer(request)


def test_replay_precedes_stale_version_with_joined_context(answer_engine: Engine) -> None:
    service = QuizDomainService(answer_engine)
    receipt = service.submit_answer(command())
    replay = service.submit_answer(replace(command(), round_version=99))
    assert replay.is_replay
    assert replay.answer_id == receipt.answer_id


def test_deadline_uses_joined_database_round_even_with_existing_identity_map(
    answer_engine: Engine,
) -> None:
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        previous_close = round_.standard_close_at
        session.execute(
            update(QuizRound)
            .where(QuizRound.id == ROUND_ID)
            .values(
                open_at=NOW - timedelta(milliseconds=14_000),
                standard_close_at=NOW - timedelta(milliseconds=4_000),
            )
            .execution_options(synchronize_session=False)
        )
        assert round_.standard_close_at == previous_close
        with pytest.raises(AnswerWindowClosedError, match="database_time_window"):
            QuizDomainService(answer_engine).submit_answer_in_transaction(
                session, command(), expected_event_id=EVENT_ID
            )
        session.rollback()
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == 0


@pytest.mark.parametrize("mode", ["accepted", "deadline", "version", "before_open", "unscheduled"])
def test_real_domain_diagnostics_distinguish_stamp_validation_and_version(
    answer_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """SQLite fixtureで実domainの計測位置とrollbackを確認し、TLS実負荷とは区別する。"""

    records: list[AnswerDiagnosticRecord] = []

    def capture(_message: str, *, record: str, **_fields: str) -> None:
        records.append(AnswerDiagnosticRecord.model_validate_json(record))

    monkeypatch.setattr("quiz_app.observability.answer_diagnostics.logfire.info", capture)
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        if mode == "deadline":
            round_.open_at = NOW - timedelta(milliseconds=14_000)
            round_.standard_close_at = NOW - timedelta(milliseconds=4_000)
        elif mode == "version":
            round_.version = 2
        elif mode == "before_open":
            round_.open_at = NOW + timedelta(seconds=1)
        elif mode == "unscheduled":
            round_.open_at = None
    diagnostic = AnswerDiagnostic("answer_post")
    diagnostic.bind(principal().session_id, CLIENT_ANSWER_ID)
    token = current_answer_diagnostic.set(diagnostic)
    try:
        service = QuizDomainService(answer_engine)
        if mode == "accepted":
            assert service.submit_answer(command()).is_replay is False
        else:
            expected = VersionConflictError if mode == "version" else AnswerWindowClosedError
            with pytest.raises(expected):
                service.submit_answer(command())
    finally:
        current_answer_diagnostic.reset(token)
    events = [record.event for record in records]
    assert events[:2] == ["answer_bound", "round_locked"]
    receipts = [record for record in records if record.event == "db_received"]
    if mode == "version":
        assert events == ["answer_bound", "round_locked"]
        assert receipts == []
    else:
        assert receipts[0].db_received_at is not None
        assert (
            receipts[0].window_position
            == {
                "accepted": "within_window",
                "deadline": "at_or_after_close",
                "before_open": "before_open",
                "unscheduled": "unavailable",
            }[mode]
        )
        assert ("window_validated" in events) is (mode == "accepted")
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == (mode == "accepted")
    serialized = "\n".join(record.model_dump_json() for record in records)
    assert str(PARTICIPANT_ID) not in serialized
    assert str(CLIENT_ANSWER_ID) not in serialized


@pytest.mark.parametrize("stored", [False, True])
def test_sync_integrity_retry_uses_fresh_session_and_only_replays_exact_answer(
    answer_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stored: bool,
) -> None:
    service = QuizDomainService(answer_engine)
    existing = service.submit_answer(command()) if stored else None
    error = IntegrityError("fixture", {}, None)

    def fail(_session: Session, _command: SubmitAnswerCommand) -> AcceptedAnswer:
        raise error

    monkeypatch.setattr(service, "submit_answer_in_transaction", fail)
    if existing is None:
        with pytest.raises(IntegrityError) as failure:
            service.submit_answer(command())
        assert failure.value is error
    else:
        assert service.submit_answer(command()).answer_id == existing.answer_id


@pytest.mark.parametrize(
    "elapsed_ms,received_ms,is_accepted",
    [
        (0, 0, True),
        (0, 13_999, True),
        (0, 14_000, False),
        (99, 99, True),
        (100, 100, True),
        (9999, 13_999, True),
        (9999, 14_000, False),
        (9999, 14_001, False),
        (10_000, 10_000, False),
        (14_999, 16_999, False),
        (14_999, 17_000, False),
        (14_999, 17_001, False),
        (15_000, 15_000, False),
        (3500, 999, False),
        (3500, 1000, True),
        (3500, 1001, True),
        (1000, 749, True),
        (1000, 750, True),
        (1000, 751, True),
        (1000, 3499, True),
        (1000, 3500, True),
        (1000, 3501, True),
        (1000, 9000, True),
        (1000, 13_999, True),
        (1000, 14_000, False),
        (2499, 13_999, True),
        (2500, 14_000, False),
    ],
)
def test_client_timing_boundaries_preserve_or_rollback_record(
    answer_engine: Engine,
    elapsed_ms: int,
    received_ms: int,
    is_accepted: bool,
) -> None:
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        participant = session.get(Participant, PARTICIPANT_ID)
        assert round_ is not None and participant is not None
        round_.open_at = NOW - timedelta(milliseconds=received_ms)
        round_.standard_close_at = round_.open_at + timedelta(seconds=10)
        round_.state = RoundState.SCHEDULED
    service = QuizDomainService(answer_engine)
    request = replace(command(), client_elapsed_ms=elapsed_ms)
    if is_accepted:
        receipt = service.submit_answer(request)
        assert receipt.client_elapsed_ms == elapsed_ms
        assert receipt.timing_basis == "client_monotonic_v1"
    else:
        with pytest.raises(AnswerWindowClosedError):
            service.submit_answer(request)
    with Session(answer_engine) as session:
        saved = session.scalar(select(Answer))
        assert (saved is not None) is is_accepted
        if saved is not None:
            assert saved.client_elapsed_ms == elapsed_ms
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None and round_.version == 1


def test_time_change_replay_conflicts_and_exact_replay_survives_publication(
    answer_engine: Engine,
) -> None:
    service = QuizDomainService(answer_engine)
    accepted = service.submit_answer(command())
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        round_.state = RoundState.REVEAL
        round_.result_publication = RoundResultPublication.PUBLISHED
        round_.version += 1
    replay = service.submit_answer(command())
    assert replay.answer_id == accepted.answer_id
    assert replay.client_elapsed_ms == 1000
    assert replay.is_replay
    with pytest.raises(AnswerContentConflictError):
        service.submit_answer(replace(command(), client_elapsed_ms=999))


@pytest.mark.parametrize(
    "state",
    [RoundState.READY_TO_REVEAL, RoundState.REVEAL, RoundState.VOIDED, RoundState.VOID_CANDIDATE],
)
def test_new_answer_is_rejected_after_publication_or_void(
    answer_engine: Engine, state: RoundState
) -> None:
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        round_.state = state
        if state is RoundState.REVEAL:
            round_.result_publication = RoundResultPublication.PUBLISHED
    with pytest.raises(InvalidStateTransitionError, match="not_accepting_answers"):
        QuizDomainService(answer_engine).submit_answer(command())
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == 0


def test_client_time_is_scored_after_fourteen_seconds_without_receipt_delay(
    answer_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_at = (NOW - timedelta(milliseconds=3500)).replace(tzinfo=None)
    with Session(answer_engine) as session, session.begin():
        round_ = session.get(QuizRound, ROUND_ID)
        assert round_ is not None
        round_.open_at = open_at
        round_.standard_close_at = open_at + timedelta(seconds=10)
    service = QuizDomainService(answer_engine)
    receipt = service.submit_answer(replace(command(), client_elapsed_ms=2000))
    assert receipt.client_elapsed_ms == 2000
    for offset in (10_000, 13_999):
        monkeypatch.setattr(
            service,
            "_database_time",
            lambda _session, elapsed=offset: open_at + timedelta(milliseconds=elapsed),
        )
        with Session(answer_engine) as session, session.begin():
            with pytest.raises(PublicationTooEarlyError):
                service.aggregate_round_results_in_transaction(session, ROUND_ID, 1)
            assert session.scalar(select(func.count()).select_from(ScoreLedger)) == 0
    monkeypatch.setattr(
        service,
        "_database_time",
        lambda _session: open_at + timedelta(milliseconds=14_000),
    )
    with Session(answer_engine) as session, session.begin():
        service.aggregate_round_results_in_transaction(session, ROUND_ID, 1)
        ledger = session.scalar(select(ScoreLedger))
        assert ledger is not None
        assert ledger.elapsed_ms == 2000
        assert ledger.speed_points == 80
        assert ledger.total_points == 480
        assert ledger.scoring_policy_version == get_quiz_settings().scoring.version


@pytest.mark.parametrize("timing_basis", ["server_received_v1", "client_monotonic_v1"])
def test_previous_event_rejects_new_answers_but_preserves_saved_exact_replay(
    answer_engine: Engine,
    timing_basis: str,
) -> None:
    other_participant_id = UUID(int=108)
    with Session(answer_engine) as session, session.begin():
        legacy = ScoringPolicy(
            version="scoring-2026-10-15-v1",
            base_points=400,
            initial_speed_bonus=100,
            speed_bucket_ms=100,
            speed_decrement_per_bucket=1,
            standard_duration_ms=10_000,
            timing_basis=timing_basis,
            arrival_grace_ms=2000 if timing_basis == "client_monotonic_v1" else 0,
            clock_tolerance_ms=250 if timing_basis == "client_monotonic_v1" else 0,
            client_ahead_tolerance_ms=250 if timing_basis == "client_monotonic_v1" else 0,
            is_approved=True,
        )
        event_ = session.get(Event, EVENT_ID)
        assert event_ is not None
        event_.scoring_policy_version = legacy.version
        session.add_all(
            [
                legacy,
                Participant(id=other_participant_id, event_id=EVENT_ID, public_label="BCD"),
                Answer(
                    participant_id=PARTICIPANT_ID,
                    round_id=ROUND_ID,
                    option_id=OPTION_ID,
                    client_answer_id=CLIENT_ANSWER_ID,
                    client_elapsed_ms=1000 if timing_basis == "client_monotonic_v1" else None,
                    received_at=NOW,
                ),
            ]
        )
    service = QuizDomainService(answer_engine)
    receipt = service.submit_answer(replace(command(), round_version=999))
    assert receipt.is_replay
    assert receipt.timing_basis == timing_basis
    assert receipt.client_elapsed_ms == (1000 if timing_basis == "client_monotonic_v1" else None)
    with pytest.raises(DomainInvariantError, match="quiz_configuration_scoring_mismatch"):
        service.submit_answer(replace(command(), participant_id=other_participant_id))
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == 1
        participant = session.get(Participant, other_participant_id)
        assert participant is not None and participant.first_answer_at is None


@pytest.mark.parametrize(
    "field,value",
    [("arrival_grace_ms", 0), ("base_points", 399), ("client_ahead_tolerance_ms", 250)],
)
def test_new_answer_rejects_mutated_current_policy_before_insert(
    answer_engine: Engine, field: str, value: int
) -> None:
    with Session(answer_engine) as session, session.begin():
        policy = session.get(ScoringPolicy, get_quiz_settings().scoring.version)
        assert policy is not None
        setattr(policy, field, value)
    with pytest.raises(DomainInvariantError, match="quiz_configuration_scoring_mismatch"):
        QuizDomainService(answer_engine).submit_answer(command())
    with Session(answer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Answer)) == 0
