from dataclasses import replace
from uuid import UUID

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.domain.types import (
    AnswerScoreInput,
    OverallStanding,
    OverallStandingInput,
    ScoredAnswer,
    ScoringRule,
)
from quiz_app.models.enums import ScoreExclusionReason, SpeedCohort


def score_answer(rule: ScoringRule, answer: AnswerScoreInput) -> ScoredAnswer:
    """version付きScoringPolicyから1回答の得点を計算する。"""

    if answer.elapsed_ms < 0:
        raise DomainInvariantError("elapsed_ms_must_be_non_negative")
    if answer.elapsed_ms >= rule.standard_duration_ms:
        raise DomainInvariantError("standard_answer_outside_scoring_window")

    speed_bucket = answer.elapsed_ms // rule.speed_bucket_ms
    base_points = rule.base_points if answer.is_correct else 0
    speed_points = 0
    if answer.is_correct:
        speed_points = max(
            0,
            rule.initial_speed_bonus - rule.speed_decrement_per_bucket * speed_bucket,
        )

    if answer.is_correct:
        speed_cohort = SpeedCohort.CORRECT
        exclusion_reason = ScoreExclusionReason.NONE
    else:
        speed_cohort = SpeedCohort.ALL_VALID
        exclusion_reason = ScoreExclusionReason.WRONG

    return ScoredAnswer(
        participant_id=answer.participant_id,
        public_label=answer.public_label,
        is_correct=answer.is_correct,
        base_points=base_points,
        speed_points=speed_points,
        total_points=base_points + speed_points,
        elapsed_ms=answer.elapsed_ms,
        speed_bucket=speed_bucket,
        speed_cohort=speed_cohort,
        exclusion_reason=exclusion_reason,
    )


def assign_speed_ranks(scores: list[ScoredAnswer]) -> list[ScoredAnswer]:
    """100ms bucket同順位の競技順位をcohort別に付与する。"""

    correct_scores = [score for score in scores if score.speed_cohort is SpeedCohort.CORRECT]
    all_valid_scores = [score for score in scores if score.speed_cohort is not SpeedCohort.EXCLUDED]
    correct_ranks = _competition_rank_by_bucket(correct_scores)
    all_valid_ranks = _competition_rank_by_bucket(all_valid_scores)

    ranked: list[ScoredAnswer] = []
    for score in scores:
        if score.speed_cohort is SpeedCohort.CORRECT:
            ranked.append(
                replace(
                    score,
                    speed_rank=correct_ranks[score.participant_id],
                    speed_cohort_size=len(correct_scores),
                )
            )
        elif score.speed_cohort is SpeedCohort.ALL_VALID:
            ranked.append(
                replace(
                    score,
                    speed_rank=all_valid_ranks[score.participant_id],
                    speed_cohort_size=len(all_valid_scores),
                )
            )
        else:
            ranked.append(score)
    return ranked


def rank_overall(standings: list[OverallStandingInput]) -> list[OverallStanding]:
    """得点・正答数・正解時刻合計で競技順位を付け、完全同率はpublic_label順に表示する。"""

    ordered = sorted(
        standings,
        key=lambda standing: (
            -standing.total_points,
            -standing.correct_count,
            standing.correct_elapsed_ms,
            standing.public_label,
        ),
    )
    ranked: list[OverallStanding] = []
    previous_key: tuple[int, int, int] | None = None
    previous_rank = 0
    for index, standing in enumerate(ordered, start=1):
        rank_key = (
            standing.total_points,
            standing.correct_count,
            standing.correct_elapsed_ms,
        )
        if rank_key != previous_key:
            previous_rank = index
            previous_key = rank_key
        ranked.append(
            OverallStanding(
                participant_id=standing.participant_id,
                public_label=standing.public_label,
                overall_rank=previous_rank,
                total_points=standing.total_points,
                correct_count=standing.correct_count,
            )
        )
    return ranked


def _competition_rank_by_bucket(scores: list[ScoredAnswer]) -> dict[UUID, int]:
    ordered = sorted(scores, key=lambda score: score.speed_bucket)
    ranks: dict[UUID, int] = {}
    previous_bucket: int | None = None
    previous_rank = 0
    for index, score in enumerate(ordered, start=1):
        if score.speed_bucket != previous_bucket:
            previous_rank = index
            previous_bucket = score.speed_bucket
        ranks[score.participant_id] = previous_rank
    return ranks
