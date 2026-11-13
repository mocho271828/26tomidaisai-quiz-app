from typing import Self, cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select
from sqlmodel import Session

import quiz_app.operator.response_times as response_times_module
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.models import Event, QuizRound, ScoreLedger, ScoringPolicy
from quiz_app.models.enums import (
    RoundResultPublication,
    RoundState,
    ScoreExclusionReason,
    SpeedCohort,
)
from quiz_app.operator.response_time_contracts import (
    OperatorResponseTimeFilters,
    OperatorResponseTimeRound,
    OperatorResponseTimeStatistics,
)
from quiz_app.operator.response_times import (
    OperatorResponseTimeService,
    ResponseTimeAnswer,
    response_time_statistics,
)

EVENT_ID = UUID(int=1)
ROUND_ID = UUID(int=2)
PAST_ROUND_ID = UUID(int=3)


def _answer(public_label: str, response_ms: int, *, correct: bool = True) -> ResponseTimeAnswer:
    return ResponseTimeAnswer(
        public_label=public_label,
        response_ms=response_ms,
        is_correct=correct,
    )


def test_actual_milliseconds_rank_independently_of_score_buckets() -> None:
    statistics = response_time_statistics(
        [
            _answer("AAC", 1_001),
            _answer("AAB", 1_000, correct=False),
            _answer("AAD", 1_000),
            _answer("AAA", 3_000),
        ]
    )

    assert [row.public_label for row in statistics.rows] == ["AAB", "AAD", "AAC", "AAA"]
    assert [row.rank for row in statistics.rows] == [1, 1, 3, 4]
    assert statistics.answer_count == 4
    assert statistics.average_response_ms == 1_500.25
    assert statistics.median_response_ms == 1_000.5
    assert statistics.fastest_response_ms == 1_000
    assert statistics.slowest_response_ms == 3_000


@pytest.mark.parametrize("times", [[], [0], [0, 200, 500]])
def test_empty_single_and_odd_number_statistics(times: list[int]) -> None:
    statistics = response_time_statistics(
        [_answer(f"AA{chr(65 + index)}", value) for index, value in enumerate(times)]
    )
    assert statistics.answer_count == len(times)
    assert statistics.median_response_ms == (times[len(times) // 2] if times else None)
    if not times:
        assert statistics.average_response_ms is None
        assert statistics.fastest_response_ms is None
        assert statistics.slowest_response_ms is None
        assert statistics.rows == []


@pytest.mark.parametrize(
    "field",
    ["average_response_ms", "median_response_ms", "fastest_response_ms", "slowest_response_ms"],
)
def test_empty_statistics_reject_non_null_metrics(field: str) -> None:
    payload = response_time_statistics([]).model_dump()
    payload[field] = 0
    with pytest.raises(ValidationError, match="null exactly when count is zero"):
        OperatorResponseTimeStatistics.model_validate(payload)


def test_statistics_reject_inconsistent_count() -> None:
    payload = response_time_statistics([_answer("AAA", 2_000)]).model_dump()
    payload["answer_count"] = 2
    with pytest.raises(ValidationError, match="number of rows"):
        OperatorResponseTimeStatistics.model_validate(payload)


def test_all_answers_and_correct_answers_have_separate_rankings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _session = _service(monkeypatch)
    result = service.get_response_times(EVENT_ID, OperatorResponseTimeFilters())
    assert result.selected_round_id == ROUND_ID
    assert [row.public_label for row in result.all_answers.rows] == ["AAC", "AAB", "AAA", "AAD"]
    assert [row.public_label for row in result.correct_answers.rows] == ["AAC", "AAA"]
    assert [row.rank for row in result.all_answers.rows] == [1, 2, 3, 4]
    assert [row.rank for row in result.correct_answers.rows] == [1, 2]


def test_can_select_past_practice_round(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _session = _service(monkeypatch)
    result = service.get_response_times(
        EVENT_ID, OperatorResponseTimeFilters(round_id=PAST_ROUND_ID)
    )
    assert result.selected_round_id == PAST_ROUND_ID
    assert result.rounds[0].is_practice
    assert result.all_answers.answer_count == 4


def test_unknown_unpublished_or_foreign_round_is_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _session = _service(monkeypatch)
    with pytest.raises(ResourceNotFoundError, match="response_time_round_not_found"):
        service.get_response_times(EVENT_ID, OperatorResponseTimeFilters(round_id=UUID(int=99)))


def test_no_published_round_returns_empty_populations(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _session = _service(monkeypatch, rounds=[])
    result = service.get_response_times(EVENT_ID, OperatorResponseTimeFilters())
    assert result.rounds == []
    assert result.selected_round_id is None
    assert result.all_answers.answer_count == 0
    assert result.correct_answers.answer_count == 0


def test_unknown_event_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session = _service(monkeypatch)
    session.event_exists = False
    with pytest.raises(ResourceNotFoundError, match="event_not_found"):
        service.get_response_times(EVENT_ID, OperatorResponseTimeFilters())


@pytest.mark.parametrize("round_number", [1, 2, 3, 4])
def test_published_round_query_uses_event_and_void_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    round_number: int,
) -> None:
    round_ = QuizRound(
        id=ROUND_ID,
        event_id=EVENT_ID,
        question_id=UUID(int=4),
        version=74,
        state=RoundState.ROUND_BACKUP_VALIDATED,
        result_publication=RoundResultPublication.PUBLISHED,
    )
    session = _Session([(round_, "4問目の問題")])
    monkeypatch.setattr(
        response_times_module, "quiz_round_number", lambda _session, _round: round_number
    )

    rounds = OperatorResponseTimeService._aggregated_rounds(cast(Session, session), EVENT_ID)

    assert rounds[0].round_number == round_number
    assert rounds[0].is_practice is (round_number == 1)
    statement = session.statement
    assert isinstance(statement, Select)
    sql = str(statement)
    assert "quiz_rounds.event_id =" in sql
    assert "questions.event_id =" in sql
    assert "quiz_rounds.result_publication =" in sql
    assert "quiz_rounds.state !=" in sql
    assert "quiz_rounds.void_reason IS NULL" in sql
    assert "ORDER BY quiz_rounds.version" in sql
    assert RoundResultPublication.PUBLISHED in statement.compile().params.values()
    assert RoundState.VOIDED in statement.compile().params.values()


def test_answers_use_saved_elapsed_not_current_participant_attributes() -> None:
    session = _Session(
        [
            (_ledger(1_001, ScoreExclusionReason.WRONG), "AAA"),
            (_ledger(9_000, ScoreExclusionReason.NONE), "AAB"),
        ]
    )
    answers = OperatorResponseTimeService._answers(cast(Session, session), EVENT_ID, ROUND_ID)

    assert [answer.response_ms for answer in answers] == [1_001, 9_000]
    statement = session.statement
    assert isinstance(statement, Select)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "score_ledgers.round_id =" in sql
    assert "participants.event_id =" in sql
    assert "participants.is_ranking_eligible" not in sql
    assert "participants.is_assisted" not in sql


@pytest.mark.parametrize("elapsed_ms", [None, -1])
def test_answers_reject_missing_or_negative_times(elapsed_ms: int | None) -> None:
    session = _Session([(_ledger(elapsed_ms, ScoreExclusionReason.NONE), "AAA")])
    with pytest.raises(DomainInvariantError, match="answer_time_data_missing"):
        OperatorResponseTimeService._answers(cast(Session, session), EVENT_ID, ROUND_ID)


def test_answers_reject_void_score_in_published_round() -> None:
    session = _Session([(_ledger(1_000, ScoreExclusionReason.VOID), "AAA")])
    with pytest.raises(DomainInvariantError, match="published_round_contains_void_score"):
        OperatorResponseTimeService._answers(cast(Session, session), EVENT_ID, ROUND_ID)


def _ledger(elapsed_ms: int | None, reason: ScoreExclusionReason) -> ScoreLedger:
    return ScoreLedger(
        round_id=ROUND_ID,
        participant_id=UUID(int=4),
        scoring_policy_version="test",
        is_correct=reason is not ScoreExclusionReason.WRONG,
        base_points=0,
        speed_points=0,
        total_points=0,
        elapsed_ms=elapsed_ms,
        speed_bucket=10,
        speed_rank=99,
        speed_cohort=SpeedCohort.EXCLUDED,
        exclusion_reason=reason,
    )


def _service(
    monkeypatch: pytest.MonkeyPatch, *, rounds: list[OperatorResponseTimeRound] | None = None
) -> tuple[OperatorResponseTimeService, "_Session"]:
    session = _Session([])
    available_rounds = (
        rounds
        if rounds is not None
        else [
            OperatorResponseTimeRound(
                round_id=PAST_ROUND_ID, round_number=1, is_practice=True, prompt="練習"
            ),
            OperatorResponseTimeRound(
                round_id=ROUND_ID, round_number=2, is_practice=False, prompt="本番1問目"
            ),
        ]
    )
    monkeypatch.setattr(response_times_module, "Session", lambda _engine: session)
    monkeypatch.setattr(response_times_module, "configure_projection_read_timeout", lambda _: None)
    monkeypatch.setattr(
        OperatorResponseTimeService,
        "_aggregated_rounds",
        staticmethod(lambda _session, _event: available_rounds),
    )
    monkeypatch.setattr(
        OperatorResponseTimeService,
        "_answers",
        staticmethod(
            lambda _session, _event, _round: [
                _answer("AAA", 3_000),
                _answer("AAB", 2_000, correct=False),
                _answer("AAC", 1_000),
                _answer("AAD", 9_000, correct=False),
            ]
        ),
    )
    return OperatorResponseTimeService(cast(Engine, object())), session


class _Session:
    def __init__(self, rows: list[tuple[object, str]]) -> None:
        self.rows = rows
        self.statement: object | None = None
        self.event_exists = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        return None

    def get(self, model: object, _id: UUID | str) -> object | None:
        if not self.event_exists:
            return None
        if model is Event:
            return Event(
                slug="fixture",
                title="Fixture",
                scoring_policy_version="fixture",
                display_name_policy_version="fixture",
            )
        if model is ScoringPolicy:
            return ScoringPolicy(
                version="fixture",
                base_points=400,
                initial_speed_bonus=100,
                speed_bucket_ms=100,
                speed_decrement_per_bucket=1,
                standard_duration_ms=10000,
            )
        raise AssertionError("unexpected_model")

    def execute(self, statement: object) -> Self:
        self.statement = statement
        return self

    def all(self) -> list[tuple[object, str]]:
        return self.rows
