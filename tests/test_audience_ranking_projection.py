from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlmodel import Session, SQLModel

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import ParticipantRankingResponse, PersonalResultRevision
from quiz_app.audience.result_batch import ResultBatchKey, load_result_batch
from quiz_app.models import Event, QuizRound, RankSnapshot, RankSnapshotRow
from quiz_app.models.enums import RankingVisibility, RoundResultPublication, RoundState
from quiz_app.quiz_configuration import ResultVisibility

EVENT_ID = UUID(int=1)
NOW = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.fixture
def ranking_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        # PostgreSQL 専用の部分 index は SQLite では条件なしになるため除外する。
        connection.execute(text("DROP INDEX uq_quiz_rounds_one_active_per_event"))
    with Session(engine) as session:
        session.add(ranking_event())
        for version in (1, 2):
            session.add(published_round(version))
            snapshot = RankSnapshot(
                id=UUID(int=version),
                event_id=EVENT_ID,
                round_id=UUID(int=10 + version),
                version=version,
                generated_at=NOW,
            )
            session.add(snapshot)
        session.add_all(
            [
                RankSnapshotRow(
                    snapshot_id=UUID(int=1),
                    public_label="ZZZ",
                    overall_rank=1,
                    total_points=9999,
                    correct_count=9,
                ),
                *[
                    RankSnapshotRow(
                        snapshot_id=UUID(int=2),
                        public_label=label,
                        overall_rank=rank,
                        total_points=points,
                        correct_count=2,
                    )
                    for label, rank, points in (
                        ("EEE", 3, 700),
                        ("DDD", 5, 600),
                        ("CCC", 3, 700),
                        ("BBB", 2, 800),
                        ("AAA", 1, 900),
                    )
                ],
            ]
        )
        session.commit()
    yield engine
    engine.dispose()


def published_round(version: int) -> QuizRound:
    return QuizRound(
        id=UUID(int=10 + version),
        event_id=EVENT_ID,
        question_id=UUID(int=20 + version),
        version=version,
        state=RoundState.ROUND_BACKUP_VALIDATED,
        result_publication=RoundResultPublication.PUBLISHED,
        created_at=NOW,
    )


def ranking_event() -> Event:
    return Event(
        id=EVENT_ID,
        slug="ranking-test",
        title="順位テスト",
        ranking_visibility=RankingVisibility.HIDDEN_ALL,
        scoring_policy_version="scoring-test",
        display_name_policy_version="display-test",
        created_at=NOW,
        updated_at=NOW,
    )


def participant_ranking(
    engine: Engine,
    label: str,
    visibility: ResultVisibility,
    version: int = 2,
) -> ParticipantRankingResponse | None:
    revision = PersonalResultRevision(
        event_id=EVENT_ID,
        event_version=1,
        result_round_id=None,
        result_round_version=None,
        ranking_snapshot_version=version if visibility.personal_ranking else None,
        result_visibility=visibility,
    )
    batch = load_result_batch(engine, ResultBatchKey.from_revision(revision))
    principal = ParticipantPrincipal(UUID(int=100), UUID(int=101), EVENT_ID, label, True)
    return batch.for_participant(principal, revision).ranking


@pytest.mark.parametrize("personal_visible", [False, True])
@pytest.mark.parametrize("overall_visible", [False, True])
def test_participant_top_rows_follow_current_visibility_and_share_snapshot(
    ranking_engine: Engine, personal_visible: bool, overall_visible: bool
) -> None:
    visibility = ResultVisibility(
        distribution=False,
        personal_ranking=personal_visible,
        overall_ranking=overall_visible,
    )
    ranking = participant_ranking(ranking_engine, "DDD", visibility)

    if not personal_visible:
        assert ranking is None
        return
    assert ranking is not None
    assert ranking.snapshot_version == 2
    assert ranking.overall_rank == 5
    assert ranking.total_points == 600
    if not overall_visible:
        assert ranking.top_rows is None
        assert "AAA" not in ranking.model_dump_json()
        return
    assert ranking.top_rows is not None
    assert [row.public_label for row in ranking.top_rows] == ["AAA", "BBB", "CCC", "EEE"]
    assert [row.overall_rank for row in ranking.top_rows] == [1, 2, 3, 3]
    assert "DDD" not in ranking.model_dump_json()
    assert "ZZZ" not in ranking.model_dump_json()


def test_top_rows_return_existing_participants_when_fewer_than_three_are_ranked(
    ranking_engine: Engine,
) -> None:
    with Session(ranking_engine) as session:
        session.add(published_round(3))
        snapshot = RankSnapshot(
            event_id=EVENT_ID, round_id=UUID(int=13), version=3, generated_at=NOW
        )
        session.add(snapshot)
        session.add(
            RankSnapshotRow(
                snapshot_id=snapshot.id,
                public_label="AAA",
                overall_rank=1,
                total_points=100,
                correct_count=1,
            )
        )
        session.commit()
    ranking = participant_ranking(
        ranking_engine,
        "AAA",
        ResultVisibility(distribution=False, personal_ranking=True, overall_ranking=True),
        version=3,
    )

    assert ranking is not None
    assert ranking.snapshot_version == 3
    assert ranking.top_rows is not None
    assert [(row.public_label, row.total_points) for row in ranking.top_rows] == [("AAA", 100)]


def test_participant_without_a_snapshot_row_has_no_ranking(ranking_engine: Engine) -> None:
    ranking = participant_ranking(
        ranking_engine,
        "NEW",
        ResultVisibility(distribution=False, personal_ranking=True, overall_ranking=True),
    )

    assert ranking is None
