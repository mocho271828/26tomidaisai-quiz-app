from datetime import UTC, datetime
from typing import Self, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select
from sqlmodel import Session

import quiz_app.operator.projection as projection_module
from quiz_app.domain.errors import DomainInvariantError, ResourceNotFoundError
from quiz_app.models.enums import RoundState
from quiz_app.models.quiz import (
    Question,
    QuestionOption,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
)
from quiz_app.operator.projection import OperatorProjectionService

NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)


def test_question_previews_load_all_options_once_and_include_correct_answers() -> None:
    event_id = UUID(int=2)
    question_a = Question(
        id=UUID(int=3),
        event_id=event_id,
        version=1,
        prompt="問題A",
        display_order=1,
    )
    question_b = Question(
        id=UUID(int=4),
        event_id=event_id,
        version=2,
        prompt="問題B",
        display_order=2,
    )
    options = [
        _preview_option(10, question_a.id, "A", display_order=1, is_correct=False),
        _preview_option(11, question_a.id, "B", display_order=2, is_correct=True),
        _preview_option(12, question_b.id, "A", display_order=1, is_correct=True),
        _preview_option(13, question_b.id, "B", display_order=2, is_correct=False),
    ]
    session = _QuestionPreviewSession(options)

    previews = OperatorProjectionService._question_previews(
        cast(Session, session),
        [question_a, question_b],
    )

    assert session.scalars_call_count == 1
    assert [preview.prompt for preview in previews] == ["問題A", "問題B"]
    assert [[option.content for option in preview.options] for preview in previews] == [
        ["選択肢A", "選択肢B"],
        ["選択肢A", "選択肢B"],
    ]
    assert [[option.is_correct for option in preview.options] for preview in previews] == [
        [False, True],
        [True, False],
    ]
    statement = session.statement
    assert isinstance(statement, Select)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "question_options.question_id IN" in sql
    assert "ORDER BY question_options.question_id, question_options.display_order" in sql
    assert [question_a.id, question_b.id] in compiled.params.values()


def test_question_previews_reject_question_without_options() -> None:
    question = Question(
        id=UUID(int=3),
        event_id=UUID(int=2),
        version=1,
        prompt="選択肢のない問題",
        display_order=1,
    )

    with pytest.raises(DomainInvariantError, match="question_options_not_found"):
        OperatorProjectionService._question_previews(
            cast(Session, _QuestionPreviewSession([])),
            [question],
        )


def _round(
    state: RoundState,
) -> QuizRound:
    return QuizRound(
        id=UUID(int=1),
        event_id=UUID(int=2),
        question_id=UUID(int=3),
        version=4,
        state=state,
    )


def test_answer_statistics_use_weighted_group_totals_and_option_order() -> None:
    option_a = _option(10, "A", display_order=1)
    option_b = _option(11, "B", display_order=2)
    option_c = _option(12, "C", display_order=3)

    statistics = OperatorProjectionService._statistics_from_option_aggregates(
        [option_a, option_b, option_c],
        {
            option_a.id: (2, 3_002),
            option_b.id: (1, 3_001),
        },
    )

    assert statistics.answer_count == 3
    assert statistics.average_response_ms == 2_001
    assert [option.option_id for option in statistics.options] == [
        option_a.id,
        option_b.id,
        option_c.id,
    ]
    assert statistics.options[0].answer_count == 2
    assert statistics.options[0].average_response_ms == 1_501
    assert statistics.options[1].answer_count == 1
    assert statistics.options[1].average_response_ms == 3_001
    assert statistics.options[2].answer_count == 0
    assert statistics.options[2].average_response_ms is None


def test_answer_statistics_query_groups_by_option_and_filters_round() -> None:
    round_ = _round(RoundState.OPEN)
    round_.open_at = NOW
    option_a = _option(10, "A", display_order=1)
    option_b = _option(11, "B", display_order=2)
    session = _StatisticsSession(
        aggregate_rows=[
            (option_a.id, 2, 3_002, 2),
            (option_b.id, 1, 3_001, 1),
        ]
    )

    statistics = OperatorProjectionService._answer_statistics(
        cast(Session, session),
        round_,
        [option_a, option_b],
    )

    assert statistics.answer_count == 3
    assert statistics.average_response_ms == 2_001
    statement = session.executed_statement
    assert isinstance(statement, Select)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "sum(CASE WHEN (scoring_policies.timing_basis" in sql
    assert "THEN answers.client_elapsed_ms ELSE floor(" in sql
    assert "count(CASE WHEN" in sql
    assert (
        "JOIN scoring_policies ON scoring_policies.version = events.scoring_policy_version" in sql
    )
    assert "WHERE answers.round_id = %(round_id_1)s::UUID" in sql
    assert "GROUP BY answers.option_id" in sql
    assert compiled.params == {
        "received_at_1": NOW,
        "timing_basis_1": "client_monotonic_v1",
        "param_1": 1_000,
        "round_id_1": round_.id,
    }


def test_answer_statistics_use_null_average_for_no_answers() -> None:
    option = _option(10, "A", display_order=1)

    statistics = OperatorProjectionService._answer_statistics(
        cast(Session, _StatisticsSession()),
        _round(RoundState.READY_TO_SCHEDULE),
        [option],
    )

    assert statistics.answer_count == 0
    assert statistics.average_response_ms is None
    assert statistics.options[0].answer_count == 0
    assert statistics.options[0].average_response_ms is None


def test_answer_statistics_reject_partial_missing_client_time() -> None:
    option = _option(10, "A", display_order=1)
    round_ = _round(RoundState.OPEN)
    round_.open_at = NOW
    session = _StatisticsSession(aggregate_rows=[(option.id, 2, 1000, 1)])
    with pytest.raises(DomainInvariantError, match="answer_time_data_missing"):
        OperatorProjectionService._answer_statistics(cast(Session, session), round_, [option])


def test_answer_statistics_reject_answer_for_another_question() -> None:
    with pytest.raises(
        DomainInvariantError,
        match="answer_option_not_found_for_question",
    ):
        OperatorProjectionService._statistics_from_option_aggregates(
            [_option(10, "A", display_order=1)],
            {UUID(int=999): (1, 1_000)},
        )


def test_answer_statistics_reject_saved_answer_without_round_open_time() -> None:
    session = _StatisticsSession(answer_count=1)

    with pytest.raises(DomainInvariantError, match="answer_time_data_missing"):
        OperatorProjectionService._answer_statistics(
            cast(Session, session),
            _round(RoundState.READY_TO_SCHEDULE),
            [_option(10, "A", display_order=1)],
        )


def test_latest_ranking_orders_rows_by_rank_then_public_label() -> None:
    snapshot = RankSnapshot(
        id=UUID(int=30),
        event_id=UUID(int=2),
        round_id=UUID(int=1),
        version=4,
    )
    rows = [
        _ranking_row(31, snapshot.id, "AAA", overall_rank=1, total_points=20_000),
        _ranking_row(33, snapshot.id, "AAB", overall_rank=1, total_points=20_000),
        _ranking_row(32, snapshot.id, "AAC", overall_rank=2, total_points=10_000),
    ]
    session = _RankingSession(snapshot, rows)

    ranking = OperatorProjectionService._latest_ranking(
        cast(Session, session),
        snapshot.event_id,
    )

    assert ranking is not None
    assert ranking.snapshot_version == 4
    assert ranking.round_id == snapshot.round_id
    assert [row.public_label for row in ranking.rows] == [
        "AAA",
        "AAB",
        "AAC",
    ]
    rows_statement = session.rows_statement
    assert isinstance(rows_statement, Select)
    assert "ORDER BY rank_snapshot_rows.overall_rank, rank_snapshot_rows.public_label" in str(
        rows_statement
    )


def test_latest_ranking_snapshot_version_uses_max_for_event() -> None:
    session = _VersionSession(version=4)

    version = OperatorProjectionService._latest_ranking_snapshot_version(
        cast(Session, session),
        UUID(int=2),
    )

    assert version == 4
    statement = session.statement
    assert isinstance(statement, Select)
    sql = str(statement)
    assert "max(rank_snapshots.version)" in sql
    assert "WHERE rank_snapshots.event_id =" in sql


def test_get_latest_ranking_rejects_unknown_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ManagedRankingSession(event=None, snapshot=None, rows=[])
    monkeypatch.setattr(projection_module, "Session", lambda _: session)
    monkeypatch.setattr(projection_module, "configure_projection_read_timeout", lambda _: None)
    service = OperatorProjectionService(cast(Engine, object()))

    with pytest.raises(ResourceNotFoundError, match="event_not_found"):
        service.get_latest_ranking(UUID(int=2))


def test_get_latest_ranking_returns_none_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ManagedRankingSession(
        event=_EventStub(UUID(int=2)),
        snapshot=None,
        rows=[],
    )
    monkeypatch.setattr(projection_module, "Session", lambda _: session)
    monkeypatch.setattr(projection_module, "configure_projection_read_timeout", lambda _: None)
    service = OperatorProjectionService(cast(Engine, object()))

    assert service.get_latest_ranking(UUID(int=2)) is None


def _option(value: int, label: str, *, display_order: int) -> QuestionOption:
    return QuestionOption(
        id=UUID(int=value),
        question_id=UUID(int=3),
        label=label,
        content=f"選択肢{label}",
        display_order=display_order,
    )


def _preview_option(
    value: int,
    question_id: UUID,
    label: str,
    *,
    display_order: int,
    is_correct: bool,
) -> QuestionOption:
    return QuestionOption(
        id=UUID(int=value),
        question_id=question_id,
        label=label,
        content=f"選択肢{label}",
        display_order=display_order,
        is_correct=is_correct,
    )


def _ranking_row(
    value: int,
    snapshot_id: UUID,
    public_label: str,
    *,
    overall_rank: int,
    total_points: int,
) -> RankSnapshotRow:
    return RankSnapshotRow(
        id=UUID(int=value),
        snapshot_id=snapshot_id,
        public_label=public_label,
        overall_rank=overall_rank,
        total_points=total_points,
        correct_count=1,
    )


class _RankingRows:
    def __init__(self, rows: list[RankSnapshotRow]) -> None:
        self._rows = rows

    def all(self) -> list[RankSnapshotRow]:
        return self._rows


class _StatisticsRows:
    def __init__(self, rows: list[tuple[UUID, int, int | None, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[UUID, int, int | None, int]]:
        return self._rows


class _QuestionOptionRows:
    def __init__(self, rows: list[QuestionOption]) -> None:
        self._rows = rows

    def all(self) -> list[QuestionOption]:
        return self._rows


class _QuestionPreviewSession:
    def __init__(self, options: list[QuestionOption]) -> None:
        self._options = options
        self.scalars_call_count = 0
        self.statement: object | None = None

    def scalars(self, statement: object) -> _QuestionOptionRows:
        self.scalars_call_count += 1
        self.statement = statement
        return _QuestionOptionRows(self._options)


class _StatisticsSession:
    def __init__(
        self,
        *,
        aggregate_rows: list[tuple[UUID, int, int | None, int]] | None = None,
        answer_count: int = 0,
    ) -> None:
        self._aggregate_rows = aggregate_rows or []
        self._answer_count = answer_count
        self.executed_statement: object | None = None

    def scalar(self, _: object) -> int:
        return self._answer_count

    def execute(self, statement: object) -> _StatisticsRows:
        self.executed_statement = statement
        return _StatisticsRows(self._aggregate_rows)


class _RankingSession:
    def __init__(
        self,
        snapshot: RankSnapshot | None,
        rows: list[RankSnapshotRow],
    ) -> None:
        self._snapshot = snapshot
        self._rows = rows
        self.rows_statement: object | None = None

    def scalar(self, _: object) -> RankSnapshot | None:
        return self._snapshot

    def scalars(self, statement: object) -> _RankingRows:
        self.rows_statement = statement
        return _RankingRows(self._rows)


class _VersionSession:
    def __init__(self, version: int | None) -> None:
        self._version = version
        self.statement: object | None = None

    def scalar(self, statement: object) -> int | None:
        self.statement = statement
        return self._version


class _ManagedRankingSession(_RankingSession):
    def __init__(
        self,
        *,
        event: object | None,
        snapshot: RankSnapshot | None,
        rows: list[RankSnapshotRow],
    ) -> None:
        super().__init__(snapshot, rows)
        self._event = event

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        return None

    def get(self, _: object, __: UUID) -> object | None:
        return self._event


class _EventStub:
    def __init__(self, event_id: UUID) -> None:
        self.id = event_id
