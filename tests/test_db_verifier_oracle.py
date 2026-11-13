from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from quiz_app.load_test.db_verifier_oracle import (
    DbVerifierOracleError,
    OracleAnswer,
    OracleHistoricalLedger,
    OracleParticipant,
    OracleScoringPolicy,
    expected_virtual_client_answer_id,
    expected_virtual_option,
    rank_overall_independently,
    score_round_independently,
)

POLICY = OracleScoringPolicy(
    version="score-v1",
    base_points=10_000,
    initial_speed_bonus=500,
    speed_bucket_ms=100,
    speed_decrement_per_bucket=10,
    standard_duration_ms=10_000,
)
OPEN_AT = datetime(2026, 9, 3, 12, tzinfo=UTC)


def _answer(
    participant: int,
    elapsed_ms: int,
    *,
    is_correct: bool = True,
    is_ranking_eligible: bool = True,
) -> OracleAnswer:
    return OracleAnswer(
        participant_id=UUID(int=participant),
        is_correct=is_correct,
        is_ranking_eligible=is_ranking_eligible,
        open_at=OPEN_AT,
        received_at=OPEN_AT + timedelta(milliseconds=elapsed_ms),
    )


@pytest.mark.parametrize(
    ("vu_id", "option"),
    [(1, "A"), (2, "B"), (496, "D"), (497, "A"), (498, "A"), (499, "B"), (500, "C")],
)
def test_fixed_virtual_answer_oracle(vu_id: int, option: str) -> None:
    assert expected_virtual_option(vu_id) == option


def test_virtual_client_answer_id_matches_approved_uuid5_fixture() -> None:
    run_id = UUID("40000000-0000-4000-8000-000000000001")
    question_id = UUID("50000000-0000-4000-8000-000000000002")

    assert str(expected_virtual_client_answer_id(run_id, question_id, 498)) == (
        "dab85870-0687-538b-9c75-40911fc043b8"
    )


def test_independent_scoring_and_competition_ranks() -> None:
    rows = score_round_independently(
        POLICY,
        (
            _answer(1, 99),
            _answer(2, 100),
            _answer(3, 100, is_correct=False),
            _answer(5, 200, is_ranking_eligible=False),
        ),
    )
    by_id = {row.participant_id.int: row for row in rows}

    assert (by_id[1].base_points, by_id[1].speed_points, by_id[1].speed_rank) == (
        10_000,
        500,
        1,
    )
    assert (by_id[2].speed_points, by_id[2].speed_rank, by_id[2].speed_cohort_size) == (
        490,
        2,
        2,
    )
    assert (by_id[3].total_points, by_id[3].speed_rank, by_id[3].speed_cohort_size) == (
        0,
        2,
        3,
    )
    assert (by_id[5].speed_points, by_id[5].speed_rank, by_id[5].exclusion_reason) == (
        480,
        None,
        "Ineligible",
    )


def test_speed_rank_uses_same_bucket_as_tie() -> None:
    rows = score_round_independently(POLICY, (_answer(1, 101), _answer(2, 199), _answer(3, 200)))
    ranks = {row.participant_id.int: row.speed_rank for row in rows}
    assert ranks == {1: 1, 2: 1, 3: 3}


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (_answer(1, -1), "oracle_answer_before_open"),
        (_answer(1, 10_000), "oracle_standard_answer_outside_window"),
    ],
)
def test_invalid_answer_time_is_rejected(answer: OracleAnswer, code: str) -> None:
    with pytest.raises(DbVerifierOracleError, match=f"^{code}$"):
        score_round_independently(POLICY, (answer,))


CLIENT_POLICY = replace(
    POLICY,
    version="score-client-v1",
    base_points=400,
    initial_speed_bonus=100,
    speed_decrement_per_bucket=1,
    timing_basis="client_monotonic_v1",
    arrival_grace_ms=2000,
    clock_tolerance_ms=250,
    client_ahead_tolerance_ms=250,
)

CURRENT_CLIENT_POLICY = replace(
    CLIENT_POLICY,
    version="scoring-2026-10-15-v2",
    arrival_grace_ms=4000,
    clock_tolerance_ms=10000,
    client_ahead_tolerance_ms=2500,
)

PREVIOUS_SEVENTEEN_AND_A_HALF_SECOND_CLIENT_POLICY = replace(
    CURRENT_CLIENT_POLICY,
    arrival_grace_ms=7500,
    clock_tolerance_ms=7500,
)

PREVIOUS_TEN_SECOND_CLIENT_POLICY = replace(
    CURRENT_CLIENT_POLICY,
    arrival_grace_ms=5000,
    clock_tolerance_ms=5000,
)

PREVIOUS_FIFTEEN_SECOND_CLIENT_POLICY = replace(
    CURRENT_CLIENT_POLICY,
    arrival_grace_ms=5000,
    clock_tolerance_ms=10000,
)


@pytest.mark.parametrize(
    ("elapsed_ms", "received_ms"),
    [
        (0, 0),
        (99, 100),
        (100, 100),
        (2000, 3500),
        (9999, 11999),
        (2000, 1750),
        (2000, 4250),
    ],
)
def test_client_oracle_uses_official_time_with_approved_arrival_bounds(
    elapsed_ms: int, received_ms: int
) -> None:
    answer = replace(_answer(1, received_ms), client_elapsed_ms=elapsed_ms)
    result = score_round_independently(CLIENT_POLICY, (answer,))[0]
    assert result.elapsed_ms == elapsed_ms
    assert result.speed_points == max(0, 100 - elapsed_ms // 100)


@pytest.mark.parametrize(
    ("elapsed_ms", "received_ms", "code"),
    [
        (10000, 10100, "oracle_client_selection_outside_window"),
        (9999, 12000, "oracle_client_arrival_outside_window"),
        (2000, 1749, "oracle_client_time_difference_invalid"),
        (2000, 4251, "oracle_client_time_difference_invalid"),
        (1000, 9000, "oracle_client_time_difference_invalid"),
        (None, 1000, "oracle_client_elapsed_invalid"),
        (-1, 1000, "oracle_client_elapsed_invalid"),
    ],
)
def test_client_oracle_rejects_missing_or_inconsistent_time(
    elapsed_ms: int | None, received_ms: int, code: str
) -> None:
    answer = replace(_answer(1, received_ms), client_elapsed_ms=elapsed_ms)
    with pytest.raises(DbVerifierOracleError, match=f"^{code}$"):
        score_round_independently(CLIENT_POLICY, (answer,))


def test_legacy_oracle_does_not_reinterpret_client_time() -> None:
    answer = replace(_answer(1, 3500), client_elapsed_ms=2000)
    with pytest.raises(DbVerifierOracleError, match="oracle_legacy_answer_has_client_elapsed"):
        score_round_independently(POLICY, (answer,))


@pytest.mark.parametrize(
    ("difference_ms", "accepted"),
    [(-2501, False), (-2500, True), (-2499, True), (2499, True), (2500, True), (2501, False)],
)
def test_client_oracle_honors_independent_saved_ahead_tolerance(
    difference_ms: int, accepted: bool
) -> None:
    policy = replace(
        CLIENT_POLICY,
        version="score-client-v2",
        client_ahead_tolerance_ms=2500,
        clock_tolerance_ms=500,
    )
    answer = replace(_answer(1, 3000 + difference_ms), client_elapsed_ms=3000)
    if accepted:
        assert score_round_independently(policy, (answer,))[0].elapsed_ms == 3000
    else:
        with pytest.raises(DbVerifierOracleError, match="^oracle_client_time_difference_invalid$"):
            score_round_independently(policy, (answer,))


@pytest.mark.parametrize(
    ("elapsed_ms", "received_ms"),
    [(0, 0), (0, 13_999), (9_999, 13_999), (3_000, 500)],
)
def test_client_oracle_accepts_current_timing_boundaries(elapsed_ms: int, received_ms: int) -> None:
    answer = replace(_answer(1, received_ms), client_elapsed_ms=elapsed_ms)
    assert score_round_independently(CURRENT_CLIENT_POLICY, (answer,))[0].elapsed_ms == elapsed_ms


@pytest.mark.parametrize(
    ("elapsed_ms", "received_ms", "code"),
    [
        (0, 14_000, "oracle_client_arrival_outside_window"),
        (9_999, 14_000, "oracle_client_arrival_outside_window"),
        (3_000, 499, "oracle_client_time_difference_invalid"),
    ],
)
def test_client_oracle_rejects_current_window_boundaries(
    elapsed_ms: int, received_ms: int, code: str
) -> None:
    answer = replace(_answer(1, received_ms), client_elapsed_ms=elapsed_ms)
    with pytest.raises(DbVerifierOracleError, match=f"^{code}$"):
        score_round_independently(CURRENT_CLIENT_POLICY, (answer,))


def test_client_oracle_preserves_previous_fifteen_second_policy_for_evidence() -> None:
    answer = replace(_answer(1, 14_999), client_elapsed_ms=0)

    previous = score_round_independently(PREVIOUS_FIFTEEN_SECOND_CLIENT_POLICY, (answer,))[0]
    assert previous.elapsed_ms == 0
    with pytest.raises(DbVerifierOracleError, match="^oracle_client_arrival_outside_window$"):
        score_round_independently(CURRENT_CLIENT_POLICY, (answer,))


def test_client_oracle_preserves_previous_seventeen_and_a_half_second_policy() -> None:
    answer = replace(_answer(1, 17_499), client_elapsed_ms=2_499)

    previous = score_round_independently(
        PREVIOUS_SEVENTEEN_AND_A_HALF_SECOND_CLIENT_POLICY, (answer,)
    )[0]
    assert previous.elapsed_ms == 2_499
    with pytest.raises(DbVerifierOracleError, match="^oracle_client_arrival_outside_window$"):
        score_round_independently(CURRENT_CLIENT_POLICY, (answer,))


def test_client_oracle_preserves_previous_ten_second_policy_for_evidence() -> None:
    answer = replace(_answer(1, 10_000), client_elapsed_ms=0)

    previous = score_round_independently(PREVIOUS_TEN_SECOND_CLIENT_POLICY, (answer,))[0]
    assert previous.elapsed_ms == 0
    current = score_round_independently(CURRENT_CLIENT_POLICY, (answer,))[0]
    assert current.elapsed_ms == 0


def test_client_oracle_current_policy_accepts_difference_rejected_by_previous_policy() -> None:
    answer = replace(_answer(1, 12_000), client_elapsed_ms=1000)

    with pytest.raises(DbVerifierOracleError, match="^oracle_client_time_difference_invalid$"):
        score_round_independently(PREVIOUS_TEN_SECOND_CLIENT_POLICY, (answer,))
    current = score_round_independently(CURRENT_CLIENT_POLICY, (answer,))[0]
    assert current.elapsed_ms == 1000


@pytest.mark.parametrize("invalid_tolerance", [0, 249, 251, 2499, 2501])
def test_client_oracle_rejects_unsupported_ahead_tolerance(invalid_tolerance: int) -> None:
    policy = replace(CLIENT_POLICY, client_ahead_tolerance_ms=invalid_tolerance)
    with pytest.raises(DbVerifierOracleError, match="^oracle_client_timing_policy_invalid$"):
        score_round_independently(policy, ())


def test_legacy_oracle_rejects_nonzero_ahead_tolerance() -> None:
    policy = replace(POLICY, client_ahead_tolerance_ms=2500)
    with pytest.raises(DbVerifierOracleError, match="^oracle_legacy_timing_policy_invalid$"):
        score_round_independently(policy, ())


def test_client_oracle_floors_received_time_only_for_difference_check() -> None:
    answer = replace(
        _answer(1, 4250),
        received_at=OPEN_AT + timedelta(milliseconds=4250, microseconds=999),
        client_elapsed_ms=2000,
    )
    assert score_round_independently(CLIENT_POLICY, (answer,))[0].elapsed_ms == 2000
    boundary_answer = replace(_answer(1, 12000), client_elapsed_ms=9999)
    with pytest.raises(DbVerifierOracleError, match="oracle_client_arrival_outside_window"):
        score_round_independently(CLIENT_POLICY, (boundary_answer,))


def test_overall_ranking_is_independent_and_competition_ranked() -> None:
    round_one = UUID(int=101)
    round_two = UUID(int=102)
    participants = (
        OracleParticipant(UUID(int=1), True),
        OracleParticipant(UUID(int=2), True),
        OracleParticipant(UUID(int=3), True),
        OracleParticipant(UUID(int=4), False),
    )
    ledgers = (
        OracleHistoricalLedger(round_one, UUID(int=1), 100, True, 200),
        OracleHistoricalLedger(round_two, UUID(int=1), 50, False, 300),
        OracleHistoricalLedger(round_one, UUID(int=2), 200, True, 200),
        OracleHistoricalLedger(round_one, UUID(int=3), 200, True, 200),
        OracleHistoricalLedger(round_one, UUID(int=4), 999, True, 1),
    )

    rows = rank_overall_independently(participants, ledgers)

    assert [(row.participant_id.int, row.overall_rank) for row in rows] == [
        (2, 1),
        (3, 1),
        (1, 3),
    ]


def test_duplicate_round_participant_is_rejected() -> None:
    with pytest.raises(DbVerifierOracleError, match="^oracle_duplicate_round_participant$"):
        score_round_independently(POLICY, (_answer(1, 100), _answer(1, 200)))


def test_virtual_answer_range_is_closed() -> None:
    for vu_id in (0, 501):
        with pytest.raises(DbVerifierOracleError, match="^oracle_virtual_vu_id_invalid$"):
            expected_virtual_option(vu_id)
