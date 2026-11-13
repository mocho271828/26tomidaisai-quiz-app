from uuid import UUID

import pytest

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.scoring import assign_speed_ranks, rank_overall, score_answer
from quiz_app.domain.types import AnswerScoreInput, OverallStandingInput, ScoringRule
from quiz_app.models.enums import ScoreExclusionReason, SpeedCohort

RULE = ScoringRule(
    version="scoring-2026-09-08-v2",
    base_points=10_000,
    initial_speed_bonus=500,
    speed_bucket_ms=100,
    speed_decrement_per_bucket=10,
    standard_duration_ms=10_000,
)


def _score_input(
    participant_number: int,
    *,
    elapsed_ms: int,
    is_correct: bool = True,
) -> AnswerScoreInput:
    return AnswerScoreInput(
        participant_id=UUID(int=participant_number),
        public_label=f"AA{chr(ord('A') + participant_number - 1)}",
        is_correct=is_correct,
        elapsed_ms=elapsed_ms,
    )


@pytest.mark.parametrize(
    ("elapsed_ms", "expected_speed_points"),
    [
        (0, 500),
        (99, 500),
        (100, 490),
        (4_900, 10),
        (4_999, 10),
        (5_000, 0),
        (9_999, 0),
    ],
)
def test_scoring_policy_boundaries(elapsed_ms: int, expected_speed_points: int) -> None:
    score = score_answer(RULE, _score_input(1, elapsed_ms=elapsed_ms))

    assert score.base_points == 10_000
    assert score.speed_points == expected_speed_points
    assert score.total_points == 10_000 + expected_speed_points


def test_standard_answer_at_close_is_not_scoreable() -> None:
    with pytest.raises(DomainInvariantError, match="outside_scoring_window"):
        score_answer(RULE, _score_input(1, elapsed_ms=10_000))


def test_wrong_answer_scores_zero_but_joins_all_valid_speed_cohort() -> None:
    score = score_answer(
        RULE,
        _score_input(1, elapsed_ms=350, is_correct=False),
    )

    assert score.total_points == 0
    assert score.speed_cohort is SpeedCohort.ALL_VALID
    assert score.exclusion_reason is ScoreExclusionReason.WRONG


def test_speed_ranking_uses_competition_rank_per_cohort() -> None:
    scores = [
        score_answer(RULE, _score_input(1, elapsed_ms=10)),
        score_answer(RULE, _score_input(2, elapsed_ms=90)),
        score_answer(RULE, _score_input(3, elapsed_ms=250)),
        score_answer(RULE, _score_input(4, elapsed_ms=150, is_correct=False)),
    ]

    by_participant = {score.participant_id: score for score in assign_speed_ranks(scores)}

    assert by_participant[UUID(int=1)].speed_rank == 1
    assert by_participant[UUID(int=2)].speed_rank == 1
    assert by_participant[UUID(int=3)].speed_rank == 3
    assert by_participant[UUID(int=3)].speed_cohort_size == 3
    assert by_participant[UUID(int=4)].speed_rank == 3
    assert by_participant[UUID(int=4)].speed_cohort_size == 4


def test_overall_ranking_does_not_use_public_label_as_tiebreaker() -> None:
    standings = rank_overall(
        [
            OverallStandingInput(UUID(int=1), "AAB", 20_000, 2, 1_000),
            OverallStandingInput(UUID(int=2), "AAA", 20_000, 2, 1_000),
            OverallStandingInput(UUID(int=3), "AAC", 10_000, 1, 500),
        ]
    )

    assert [standing.public_label for standing in standings] == ["AAA", "AAB", "AAC"]
    assert [standing.overall_rank for standing in standings] == [1, 1, 3]
