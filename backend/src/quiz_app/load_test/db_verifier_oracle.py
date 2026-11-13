from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid5

SpeedCohortValue = Literal["Correct", "AllValid", "Excluded"]
ExclusionReasonValue = Literal["None", "Wrong", "Ineligible"]
OptionLabel = Literal["A", "B", "C", "D"]


class DbVerifierOracleError(ValueError):
    """入力値を含めず、独立oracleの固定失敗codeだけを表す。"""


@dataclass(frozen=True, slots=True)
class OracleScoringPolicy:
    version: str
    base_points: int
    initial_speed_bonus: int
    speed_bucket_ms: int
    speed_decrement_per_bucket: int
    standard_duration_ms: int
    timing_basis: Literal["server_received_v1", "client_monotonic_v1"] = "server_received_v1"
    arrival_grace_ms: int = 0
    clock_tolerance_ms: int = 0
    client_ahead_tolerance_ms: int = 0


@dataclass(frozen=True, slots=True)
class OracleAnswer:
    participant_id: UUID
    is_correct: bool
    is_ranking_eligible: bool
    open_at: datetime
    received_at: datetime
    client_elapsed_ms: int | None = None


@dataclass(frozen=True, slots=True)
class OracleScoreLedger:
    participant_id: UUID
    scoring_policy_version: str
    is_correct: bool
    base_points: int
    speed_points: int
    total_points: int
    elapsed_ms: int
    speed_bucket: int
    speed_rank: int | None
    speed_cohort_size: int | None
    speed_cohort: SpeedCohortValue
    exclusion_reason: ExclusionReasonValue


@dataclass(frozen=True, slots=True)
class OracleParticipant:
    participant_id: UUID
    is_ranking_eligible: bool


@dataclass(frozen=True, slots=True)
class OracleHistoricalLedger:
    round_id: UUID
    participant_id: UUID
    total_points: int
    is_correct: bool
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class OracleOverallRank:
    participant_id: UUID
    overall_rank: int
    total_points: int
    correct_count: int


@dataclass(slots=True)
class _StandingAccumulator:
    participant_id: UUID
    total_points: int = 0
    correct_count: int = 0
    correct_elapsed_ms: int = 0


def expected_virtual_option(vu_id: int) -> OptionLabel:
    """固定600 VU契約から、回答対象VUの選択肢を独立に導出する。"""

    if 1 <= vu_id <= 496:
        return ("A", "B", "C", "D")[(vu_id - 1) % 4]
    fixed: dict[int, OptionLabel] = {497: "A", 498: "A", 499: "B", 500: "C", 599: "B", 600: "C"}
    try:
        return fixed[vu_id]
    except KeyError as error:
        raise DbVerifierOracleError("oracle_virtual_vu_id_invalid") from error


def expected_answering_vu_ids(population: Literal["mixed", "concentrated"]) -> tuple[int, ...]:
    """各問の500回答者をgeneratorと独立に固定し、参加者の入替えを検出する。"""

    if population == "mixed":
        return (*range(1, 499), 599, 600)
    return tuple(range(1, 501))


def expected_virtual_client_answer_id(
    run_id: UUID,
    question_id: UUID,
    vu_id: int,
) -> UUID:
    """generator契約と同じnameを使い、保存済み回答を個人情報なしで分類する。"""

    expected_virtual_option(vu_id)
    return uuid5(run_id, f"{question_id}:{vu_id}")


def score_round_independently(
    policy: OracleScoringPolicy,
    answers: tuple[OracleAnswer, ...],
) -> tuple[OracleScoreLedger, ...]:
    """production scoring実装をimportせず、同じ永続化契約を再計算する。"""

    _validate_policy(policy)
    participant_ids = tuple(answer.participant_id for answer in answers)
    if len(participant_ids) != len(set(participant_ids)):
        raise DbVerifierOracleError("oracle_duplicate_round_participant")

    unranked = tuple(_score_answer(policy, answer) for answer in answers)
    correct = tuple(row for row in unranked if row.speed_cohort == "Correct")
    all_valid = tuple(row for row in unranked if row.speed_cohort != "Excluded")
    correct_ranks = _competition_ranks(correct)
    all_valid_ranks = _competition_ranks(all_valid)

    ranked: list[OracleScoreLedger] = []
    for row in unranked:
        if row.speed_cohort == "Correct":
            ranked.append(
                replace(
                    row,
                    speed_rank=correct_ranks[row.participant_id],
                    speed_cohort_size=len(correct),
                )
            )
        elif row.speed_cohort == "AllValid":
            ranked.append(
                replace(
                    row,
                    speed_rank=all_valid_ranks[row.participant_id],
                    speed_cohort_size=len(all_valid),
                )
            )
        else:
            ranked.append(row)
    return tuple(sorted(ranked, key=lambda row: row.participant_id.bytes))


def rank_overall_independently(
    participants: tuple[OracleParticipant, ...],
    ledgers: tuple[OracleHistoricalLedger, ...],
) -> tuple[OracleOverallRank, ...]:
    """snapshot生成処理を使わず、累積得点の競技順位を再計算する。"""

    participant_by_id: dict[UUID, OracleParticipant] = {}
    for participant in participants:
        if participant.participant_id in participant_by_id:
            raise DbVerifierOracleError("oracle_duplicate_participant")
        participant_by_id[participant.participant_id] = participant

    seen_round_participants: set[tuple[UUID, UUID]] = set()
    accumulators: dict[UUID, _StandingAccumulator] = {}
    for ledger in ledgers:
        pair = (ledger.round_id, ledger.participant_id)
        if pair in seen_round_participants:
            raise DbVerifierOracleError("oracle_duplicate_historical_ledger")
        seen_round_participants.add(pair)
        participant = participant_by_id.get(ledger.participant_id)
        if participant is None:
            raise DbVerifierOracleError("oracle_ledger_participant_missing")
        if ledger.total_points < 0 or ledger.elapsed_ms < 0:
            raise DbVerifierOracleError("oracle_historical_ledger_invalid")
        if not participant.is_ranking_eligible:
            continue
        accumulator = accumulators.setdefault(
            participant.participant_id,
            _StandingAccumulator(participant_id=participant.participant_id),
        )
        accumulator.total_points += ledger.total_points
        if ledger.is_correct:
            accumulator.correct_count += 1
            accumulator.correct_elapsed_ms += ledger.elapsed_ms

    ordered = sorted(
        accumulators.values(),
        key=lambda row: (
            -row.total_points,
            -row.correct_count,
            row.correct_elapsed_ms,
            row.participant_id.bytes,
        ),
    )
    result: list[OracleOverallRank] = []
    previous_key: tuple[int, int, int] | None = None
    previous_rank = 0
    for index, row in enumerate(ordered, start=1):
        rank_key = (row.total_points, row.correct_count, row.correct_elapsed_ms)
        if rank_key != previous_key:
            previous_key = rank_key
            previous_rank = index
        result.append(
            OracleOverallRank(
                participant_id=row.participant_id,
                overall_rank=previous_rank,
                total_points=row.total_points,
                correct_count=row.correct_count,
            )
        )
    return tuple(result)


def _validate_policy(policy: OracleScoringPolicy) -> None:
    if (
        not policy.version
        or policy.base_points < 0
        or policy.initial_speed_bonus < 0
        or policy.speed_bucket_ms <= 0
        or policy.speed_decrement_per_bucket < 0
        or policy.standard_duration_ms <= 0
    ):
        raise DbVerifierOracleError("oracle_scoring_policy_invalid")
    if policy.timing_basis == "server_received_v1":
        if (
            policy.arrival_grace_ms != 0
            or policy.clock_tolerance_ms != 0
            or policy.client_ahead_tolerance_ms != 0
        ):
            raise DbVerifierOracleError("oracle_legacy_timing_policy_invalid")
    elif policy.timing_basis == "client_monotonic_v1":
        timing_values = (
            policy.standard_duration_ms,
            policy.arrival_grace_ms,
            policy.clock_tolerance_ms,
            policy.client_ahead_tolerance_ms,
        )
        if timing_values not in (
            (10000, 2000, 250, 250),
            (10000, 2000, 500, 2500),
            (10000, 5000, 10000, 2500),
            (10000, 5000, 5000, 2500),
            (10000, 7500, 7500, 2500),
            (10000, 4000, 10000, 2500),
        ):
            raise DbVerifierOracleError("oracle_client_timing_policy_invalid")
    else:
        raise DbVerifierOracleError("oracle_timing_basis_unknown")


def _score_answer(
    policy: OracleScoringPolicy,
    answer: OracleAnswer,
) -> OracleScoreLedger:
    if (
        answer.open_at.tzinfo is None
        or answer.open_at.utcoffset() is None
        or answer.received_at.tzinfo is None
        or answer.received_at.utcoffset() is None
    ):
        raise DbVerifierOracleError("oracle_answer_time_invalid")
    received_ms = (answer.received_at - answer.open_at) // timedelta(milliseconds=1)
    if received_ms < 0:
        raise DbVerifierOracleError("oracle_answer_before_open")
    if policy.timing_basis == "client_monotonic_v1":
        elapsed_ms = answer.client_elapsed_ms
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise DbVerifierOracleError("oracle_client_elapsed_invalid")
        duration_ms = policy.standard_duration_ms
        if elapsed_ms >= duration_ms:
            raise DbVerifierOracleError("oracle_client_selection_outside_window")
        if received_ms >= duration_ms + policy.arrival_grace_ms:
            raise DbVerifierOracleError("oracle_client_arrival_outside_window")
        if (
            not -policy.client_ahead_tolerance_ms
            <= received_ms - elapsed_ms
            <= (policy.arrival_grace_ms + policy.clock_tolerance_ms)
        ):
            raise DbVerifierOracleError("oracle_client_time_difference_invalid")
    else:
        if answer.client_elapsed_ms is not None:
            raise DbVerifierOracleError("oracle_legacy_answer_has_client_elapsed")
        elapsed_ms = received_ms
        if elapsed_ms >= policy.standard_duration_ms:
            raise DbVerifierOracleError("oracle_standard_answer_outside_window")

    speed_bucket = elapsed_ms // policy.speed_bucket_ms
    base_points = policy.base_points if answer.is_correct else 0
    speed_points = 0
    if answer.is_correct:
        speed_points = max(
            0,
            policy.initial_speed_bonus - policy.speed_decrement_per_bucket * speed_bucket,
        )

    if not answer.is_ranking_eligible:
        speed_cohort: SpeedCohortValue = "Excluded"
        exclusion_reason: ExclusionReasonValue = "Ineligible"
    elif answer.is_correct:
        speed_cohort = "Correct"
        exclusion_reason = "None"
    else:
        speed_cohort = "AllValid"
        exclusion_reason = "Wrong"

    return OracleScoreLedger(
        participant_id=answer.participant_id,
        scoring_policy_version=policy.version,
        is_correct=answer.is_correct,
        base_points=base_points,
        speed_points=speed_points,
        total_points=base_points + speed_points,
        elapsed_ms=elapsed_ms,
        speed_bucket=speed_bucket,
        speed_rank=None,
        speed_cohort_size=None,
        speed_cohort=speed_cohort,
        exclusion_reason=exclusion_reason,
    )


def _competition_ranks(rows: tuple[OracleScoreLedger, ...]) -> dict[UUID, int]:
    ordered = sorted(rows, key=lambda row: (row.speed_bucket, row.participant_id.bytes))
    ranks: dict[UUID, int] = {}
    previous_bucket: int | None = None
    previous_rank = 0
    for index, row in enumerate(ordered, start=1):
        if row.speed_bucket != previous_bucket:
            previous_bucket = row.speed_bucket
            previous_rank = index
        ranks[row.participant_id] = previous_rank
    return ranks
