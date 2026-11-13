from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from quiz_app.domain.errors import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from quiz_app.domain.quiz_service import QuizDomainService
from quiz_app.models import (
    Answer,
    Event,
    Question,
    QuestionOption,
    QuizRound,
    ReadinessReport,
    RoundPreparationTarget,
    ScoreLedger,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    EventState,
    RoundResultPublication,
    RoundState,
    SpeedCohort,
)
from quiz_app.quiz_configuration import EventConfiguration

pytestmark = pytest.mark.usefixtures("legacy_quiz_configuration")

EVENT_ID = UUID("72000000-0000-4000-8000-000000000001")
FOREIGN_EVENT_ID = UUID("72000000-0000-4000-8000-000000000002")
OLD_ROUND_ID = UUID("72000000-0000-4000-8000-000000000003")
OLD_QUESTION_ID = UUID("72000000-0000-4000-8000-000000000004")
NEXT_QUESTION_ID = UUID("72000000-0000-4000-8000-000000000005")
NEXT_OPTION_ID = UUID("72000000-0000-4000-8000-000000000006")
PARTICIPANT_ID = UUID("72000000-0000-4000-8000-000000000007")
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

RoundScopedRecord = Answer | ScoreLedger | ReadinessReport | RoundPreparationTarget


class FakeScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class FakeProgressionSession:
    def __init__(
        self,
        *,
        questions: list[Question] | None = None,
        scalar_values: list[object] | None = None,
        scalar_rows: list[list[object]] | None = None,
        existing_records: list[RoundScopedRecord] | None = None,
    ) -> None:
        self.questions = {question.id: question for question in questions or []}
        self.scalar_values = list(scalar_values or [])
        self.scalar_rows = list(scalar_rows or [])
        self.records: list[object] = list(existing_records or [])
        self.added: list[object] = []
        self.executed_statements: list[object] = []
        self.flush_count = 0

    def get(self, model_type: type[Question], primary_key: UUID) -> Question | None:
        if model_type is Question:
            return self.questions.get(primary_key)
        return None

    def scalar(self, statement: object) -> object:
        if "FROM scoring_policies" in str(statement):
            return ScoringPolicy(
                version="score-v1",
                base_points=100,
                initial_speed_bonus=20,
                speed_bucket_ms=100,
                speed_decrement_per_bucket=1,
                standard_duration_ms=10_000,
                is_approved=True,
            )
        if not self.scalar_values:
            raise AssertionError("unexpected scalar query")
        return self.scalar_values.pop(0)

    def scalars(self, _: object) -> FakeScalarRows:
        if not self.scalar_rows:
            raise AssertionError("unexpected scalars query")
        return FakeScalarRows(self.scalar_rows.pop(0))

    def add(self, record: object) -> None:
        self.added.append(record)
        self.records.append(record)

    def execute(self, statement: object) -> None:
        self.executed_statements.append(statement)

    def flush(self) -> None:
        self.flush_count += 1


def _event(
    *,
    state: EventState = EventState.IN_PROGRESS,
    version: int = 12,
) -> Event:
    return Event(
        id=EVENT_ID,
        slug="round-progression",
        title="[DEMO] Round progression",
        state=state,
        scoring_policy_version="score-v1",
        display_name_policy_version="name-v1",
        version=version,
    )


def _round(
    *,
    state: RoundState = RoundState.ROUND_BACKUP_PENDING,
    version: int = 8,
) -> QuizRound:
    return QuizRound(
        id=OLD_ROUND_ID,
        event_id=EVENT_ID,
        question_id=OLD_QUESTION_ID,
        version=version,
        state=state,
        result_publication=RoundResultPublication.PUBLISHED,
        revealed_at=NOW,
    )


def _question(
    question_id: UUID = NEXT_QUESTION_ID,
    *,
    event_id: UUID = EVENT_ID,
) -> Question:
    return Question(
        id=question_id,
        event_id=event_id,
        version=2,
        prompt="[DEMO] 次問",
        display_order=2,
    )


def _options(question_id: UUID = NEXT_QUESTION_ID) -> list[QuestionOption]:
    return [
        QuestionOption(
            id=NEXT_OPTION_ID,
            question_id=question_id,
            label="A",
            content="正解",
            display_order=0,
            is_correct=True,
        ),
        QuestionOption(
            id=UUID(int=NEXT_OPTION_ID.int + 1),
            question_id=question_id,
            label="B",
            content="不正解",
            display_order=1,
            is_correct=False,
        ),
    ]


def _service(
    monkeypatch: pytest.MonkeyPatch,
    round_: QuizRound,
    event: Event,
) -> QuizDomainService:
    service = QuizDomainService(cast(Engine, object()))
    monkeypatch.setattr(service, "_round_for_update", lambda _session, _id: round_)
    monkeypatch.setattr(service, "_event_for_update", lambda _session, _id: event)
    monkeypatch.setattr(service, "_database_time", lambda _session: NOW)
    return service


def _old_round_records() -> list[RoundScopedRecord]:
    return [
        Answer(
            participant_id=PARTICIPANT_ID,
            round_id=OLD_ROUND_ID,
            option_id=UUID(int=NEXT_OPTION_ID.int + 2),
            client_answer_id=UUID(int=NEXT_OPTION_ID.int + 3),
        ),
        ScoreLedger(
            round_id=OLD_ROUND_ID,
            participant_id=PARTICIPANT_ID,
            scoring_policy_version="score-v1",
            is_correct=True,
            base_points=100,
            speed_points=20,
            total_points=120,
            speed_cohort=SpeedCohort.CORRECT,
        ),
        ReadinessReport(
            participant_id=PARTICIPANT_ID,
            round_id=OLD_ROUND_ID,
            round_version=8,
            is_ready=True,
        ),
        RoundPreparationTarget(
            round_id=OLD_ROUND_ID,
            participant_id=PARTICIPANT_ID,
        ),
    ]


def test_schedule_round_uses_validated_exact_target_open_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.READY_TO_SCHEDULE, version=6)
    event = _event()
    session = FakeProgressionSession(scalar_values=[1, 1, 0])
    service = _service(monkeypatch, round_, event)
    target_open_at = NOW + timedelta(seconds=3)

    result = service.schedule_round_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        expected_version=6,
        target_open_at=target_open_at,
    )

    assert result.open_at == target_open_at
    assert result.standard_close_at == target_open_at + timedelta(seconds=10)
    assert round_.state is RoundState.SCHEDULED


@pytest.mark.parametrize("lead_seconds", [1, 5])
def test_schedule_round_rejects_target_outside_transaction_window(
    monkeypatch: pytest.MonkeyPatch,
    lead_seconds: int,
) -> None:
    round_ = _round(state=RoundState.READY_TO_SCHEDULE, version=6)
    event = _event()
    session = FakeProgressionSession(scalar_values=[1, 1, 0])
    service = _service(monkeypatch, round_, event)

    with pytest.raises(
        InvalidStateTransitionError,
        match="round_target_open_at_out_of_range",
    ):
        service.schedule_round_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            expected_version=6,
            target_open_at=NOW + timedelta(seconds=lead_seconds),
        )

    assert round_.state is RoundState.READY_TO_SCHEDULE
    assert round_.version == 6
    assert not session.executed_statements


def test_begin_round_backup_moves_reveal_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.REVEAL, version=6)
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    result = service.begin_round_backup_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        expected_version=6,
    )

    assert round_.state is RoundState.ROUND_BACKUP_PENDING
    assert round_.version == 7
    assert result.round_state is RoundState.ROUND_BACKUP_PENDING
    assert result.round_version == 7
    assert session.executed_statements == []
    assert session.flush_count == 1
    assert event.version == 12


def test_begin_round_backup_retries_failed_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.ROUND_BACKUP_FAILED, version=7)
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    result = service.begin_round_backup_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        expected_version=7,
    )

    assert round_.state is RoundState.ROUND_BACKUP_PENDING
    assert round_.version == 9
    assert result.round_version == 9
    assert session.executed_statements == []


def test_begin_round_backup_rejects_stale_version_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.REVEAL, version=6)
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    with pytest.raises(VersionConflictError, match="round_version_conflict"):
        service.begin_round_backup_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            expected_version=5,
        )

    assert round_.state is RoundState.REVEAL
    assert round_.version == 6
    assert event.version == 12
    assert not session.executed_statements


def test_begin_round_backup_rejects_unpublished_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.REVEAL, version=6)
    round_.result_publication = RoundResultPublication.WITHHELD
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    with pytest.raises(InvalidStateTransitionError, match="round_results_are_not_published"):
        service.begin_round_backup_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            expected_version=6,
        )

    assert round_.state is RoundState.REVEAL
    assert round_.version == 6
    assert event.version == 12
    assert not session.executed_statements


def test_mark_result_failure_and_operator_retry_preserve_withheld_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.AWAITING_VALIDATION, version=6)
    round_.result_publication = RoundResultPublication.WITHHELD
    round_.standard_close_at = NOW
    event = _event(version=12)
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    failed = service.mark_round_results_void_candidate_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        expected_version=6,
        failure_reason="automatic_aggregate_domain_failure:invalid_fixture",
    )

    assert failed.round_state is RoundState.VOID_CANDIDATE
    assert round_.state is RoundState.VOID_CANDIDATE
    assert round_.result_publication is RoundResultPublication.WITHHELD
    assert round_.void_reason == "automatic_aggregate_domain_failure:invalid_fixture"
    assert round_.version == 7
    assert event.version == 13

    retried = service.retry_round_results_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        expected_version=7,
    )

    assert retried.round_state is RoundState.AWAITING_VALIDATION
    assert round_.state is RoundState.AWAITING_VALIDATION
    assert round_.result_publication is RoundResultPublication.WITHHELD
    assert round_.void_reason is None
    assert round_.version == 8
    assert event.version == 14


def test_result_retry_rejects_non_void_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(state=RoundState.AWAITING_VALIDATION, version=6)
    round_.result_publication = RoundResultPublication.WITHHELD
    round_.standard_close_at = NOW
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    with pytest.raises(
        InvalidStateTransitionError,
        match="round_is_not_retryable_for_results",
    ):
        service.retry_round_results_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            expected_version=6,
        )

    assert round_.state is RoundState.AWAITING_VALIDATION
    assert round_.version == 6
    assert event.version == 12


@pytest.mark.parametrize(
    ("event_state", "publication", "expected_error"),
    [
        (
            EventState.IN_PROGRESS,
            RoundResultPublication.PUBLISHED,
            "round_results_are_already_published",
        ),
        (
            EventState.COMPLETED,
            RoundResultPublication.WITHHELD,
            "event_is_not_in_progress",
        ),
    ],
)
def test_result_aggregation_rechecks_locked_event_and_publication(
    monkeypatch: pytest.MonkeyPatch,
    event_state: EventState,
    publication: RoundResultPublication,
    expected_error: str,
) -> None:
    round_ = _round(state=RoundState.AWAITING_VALIDATION, version=6)
    round_.result_publication = publication
    round_.standard_close_at = NOW
    event = _event(state=event_state)
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    with pytest.raises(InvalidStateTransitionError, match=expected_error):
        service.aggregate_round_results_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            expected_version=6,
        )

    assert round_.state is RoundState.AWAITING_VALIDATION
    assert round_.version == 6


def test_fail_round_backup_persists_failure_and_blocks_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(version=8)
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    result = service.fail_round_backup_in_transaction(cast(Session, session), OLD_ROUND_ID)

    assert round_.state is RoundState.ROUND_BACKUP_FAILED
    assert round_.version == 9
    assert result.round_state is RoundState.ROUND_BACKUP_FAILED
    assert result.round_version == 9
    assert event.version == 13
    assert event.updated_at == NOW
    with pytest.raises(InvalidStateTransitionError, match="round_backup_is_not_pending"):
        service.advance_round_after_backup_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            NEXT_QUESTION_ID,
        )
    assert not session.added


def test_advance_round_creates_fresh_isolated_active_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(version=8)
    event = _event(version=12)
    old_records = _old_round_records()
    session = FakeProgressionSession(
        questions=[_question()],
        scalar_values=[11],
        scalar_rows=[[OLD_QUESTION_ID], _options()],
        existing_records=old_records,
    )
    service = _service(monkeypatch, round_, event)

    result = service.advance_round_after_backup_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        NEXT_QUESTION_ID,
    )

    assert round_.state is RoundState.ROUND_BACKUP_VALIDATED
    assert round_.version == 9
    assert len(session.added) == 1
    next_round = session.added[0]
    assert isinstance(next_round, QuizRound)
    assert next_round.id != OLD_ROUND_ID
    assert next_round.event_id == EVENT_ID
    assert next_round.question_id == NEXT_QUESTION_ID
    assert next_round.version == 12
    assert next_round.state is RoundState.PREPARING_ASSETS
    assert next_round.result_publication is RoundResultPublication.WITHHELD
    assert next_round.open_at is None
    assert next_round.standard_close_at is None
    assert next_round.revealed_at is None
    assert event.version == 13
    assert event.updated_at == NOW
    assert result.previous_round_id == OLD_ROUND_ID
    assert result.new_round_id == next_round.id
    assert result.new_round_version == 12

    round_scoped_types = (Answer, ScoreLedger, ReadinessReport, RoundPreparationTarget)
    round_scoped_records = [
        record for record in session.records if isinstance(record, round_scoped_types)
    ]
    assert round_scoped_records == old_records
    assert all(record.round_id == OLD_ROUND_ID for record in round_scoped_records)
    assert not any(record.round_id == next_round.id for record in round_scoped_records)

    active_rounds = [
        candidate
        for candidate in (round_, next_round)
        if candidate.state not in {RoundState.VOIDED, RoundState.ROUND_BACKUP_VALIDATED}
    ]
    assert active_rounds == [next_round]

    with pytest.raises(InvalidStateTransitionError, match="round_backup_is_not_pending"):
        service.advance_round_after_backup_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            NEXT_QUESTION_ID,
        )
    assert session.added == [next_round]


def test_advance_round_rejects_used_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round()
    event = _event()
    session = FakeProgressionSession(
        questions=[_question()],
        scalar_rows=[[OLD_QUESTION_ID, NEXT_QUESTION_ID]],
    )
    service = _service(monkeypatch, round_, event)

    with pytest.raises(InvalidStateTransitionError, match="question_was_already_used"):
        service.advance_round_after_backup_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            NEXT_QUESTION_ID,
        )

    assert round_.state is RoundState.ROUND_BACKUP_PENDING
    assert event.version == 12
    assert not session.added


def test_advance_round_rejects_foreign_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round()
    event = _event()
    session = FakeProgressionSession(
        questions=[_question(event_id=FOREIGN_EVENT_ID)],
    )
    service = _service(monkeypatch, round_, event)

    with pytest.raises(ResourceNotFoundError, match="question_not_found_for_event"):
        service.advance_round_after_backup_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            NEXT_QUESTION_ID,
        )

    assert round_.state is RoundState.ROUND_BACKUP_PENDING
    assert not session.added


@pytest.mark.parametrize(
    ("operation", "round_state", "expected_message"),
    [
        ("begin", RoundState.ROUND_BACKUP_PENDING, "round_is_not_ready_for_backup"),
        ("fail", RoundState.REVEAL, "round_backup_is_not_pending"),
        ("advance", RoundState.REVEAL, "round_backup_is_not_pending"),
        ("complete", RoundState.REVEAL, "round_backup_is_not_pending"),
    ],
)
def test_round_progression_rejects_wrong_round_state(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    round_state: RoundState,
    expected_message: str,
) -> None:
    round_ = _round(state=round_state)
    event = _event()
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    with pytest.raises(InvalidStateTransitionError, match=expected_message):
        if operation == "begin":
            service.begin_round_backup_in_transaction(
                cast(Session, session),
                OLD_ROUND_ID,
                expected_version=round_.version,
            )
        elif operation == "fail":
            service.fail_round_backup_in_transaction(cast(Session, session), OLD_ROUND_ID)
        elif operation == "advance":
            service.advance_round_after_backup_in_transaction(
                cast(Session, session),
                OLD_ROUND_ID,
                NEXT_QUESTION_ID,
            )
        else:
            service.complete_event_after_backup_in_transaction(cast(Session, session), OLD_ROUND_ID)

    assert round_.state is round_state
    assert event.version == 12
    assert not session.added


def test_complete_event_allows_unused_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round()
    event = _event()
    session = FakeProgressionSession(questions=[_question()])
    service = _service(monkeypatch, round_, event)

    service.complete_event_after_backup_in_transaction(cast(Session, session), OLD_ROUND_ID)

    assert NEXT_QUESTION_ID in session.questions
    assert round_.state is RoundState.ROUND_BACKUP_VALIDATED
    assert event.state is EventState.COMPLETED
    assert event.version == 13


def test_complete_event_validates_backup_and_bumps_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_ = _round(version=8)
    event = _event(version=12)
    session = FakeProgressionSession()
    service = _service(monkeypatch, round_, event)

    result = service.complete_event_after_backup_in_transaction(
        cast(Session, session), OLD_ROUND_ID
    )

    assert round_.state is RoundState.ROUND_BACKUP_VALIDATED
    assert round_.version == 9
    assert event.state is EventState.COMPLETED
    assert event.version == 13
    assert event.updated_at == NOW
    assert result.round_state is RoundState.ROUND_BACKUP_VALIDATED
    assert result.round_version == 9
    assert result.event_version == 13
    assert not session.added


@pytest.mark.parametrize(
    ("delay_seconds", "target_lead"),
    [(1, None), (3, None), (7, None), (1, 1), (1, 2), (3, 2), (3, 4), (7, 6), (7, 8)],
)
def test_schedule_derives_positive_window_and_default_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
    legacy_quiz_configuration: EventConfiguration,
    delay_seconds: int,
    target_lead: int | None,
) -> None:
    configured = EventConfiguration(
        settings=legacy_quiz_configuration.settings.model_copy(
            update={"start_delay_seconds": delay_seconds}
        ),
        record=legacy_quiz_configuration.record,
    )
    monkeypatch.setattr(
        "quiz_app.domain.quiz_service.require_event_configuration",
        lambda _session, _event: configured,
    )
    round_ = _round(state=RoundState.READY_TO_SCHEDULE, version=6)
    session = FakeProgressionSession(scalar_values=[1, 1, 0])
    service = _service(monkeypatch, round_, _event())
    result = service.schedule_round_in_transaction(
        cast(Session, session),
        OLD_ROUND_ID,
        expected_version=6,
        target_open_at=None if target_lead is None else NOW + timedelta(seconds=target_lead),
    )
    assert result.open_at == NOW + timedelta(
        seconds=delay_seconds if target_lead is None else target_lead
    )


def test_one_second_configuration_never_allows_present_or_past_start(
    monkeypatch: pytest.MonkeyPatch,
    legacy_quiz_configuration: EventConfiguration,
) -> None:
    configured = EventConfiguration(
        settings=legacy_quiz_configuration.settings.model_copy(update={"start_delay_seconds": 1}),
        record=legacy_quiz_configuration.record,
    )
    monkeypatch.setattr(
        "quiz_app.domain.quiz_service.require_event_configuration",
        lambda _session, _event: configured,
    )
    round_ = _round(state=RoundState.READY_TO_SCHEDULE, version=6)
    session = FakeProgressionSession(scalar_values=[1, 1, 0])
    service = _service(monkeypatch, round_, _event())
    with pytest.raises(InvalidStateTransitionError, match="round_target_open_at_out_of_range"):
        service.schedule_round_in_transaction(
            cast(Session, session),
            OLD_ROUND_ID,
            expected_version=6,
            target_open_at=NOW,
        )
