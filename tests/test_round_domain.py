from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from quiz_app.answer_timing import AnswerTiming
from quiz_app.domain.errors import AnswerWindowClosedError, PublicationTooEarlyError
from quiz_app.domain.rounds import (
    CLOCK_PHASE_BY_STATE,
    resolve_timed_round_state,
    round_state_version,
    validate_answer_window,
    validate_publication_time,
)
from quiz_app.models.enums import RoundState
from quiz_app.models.quiz import QuizRound

OPEN_AT = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
CLIENT_TIMING = AnswerTiming(
    basis="client_monotonic_v1",
    arrival_grace_ms=4000,
    clock_tolerance_ms=10000,
    client_ahead_tolerance_ms=2500,
)


def _round(state: RoundState = RoundState.SCHEDULED) -> QuizRound:
    return QuizRound(
        id=UUID(int=1),
        event_id=UUID(int=2),
        question_id=UUID(int=3),
        version=4,
        state=state,
        open_at=OPEN_AT,
        standard_close_at=OPEN_AT + timedelta(seconds=10),
    )


@pytest.mark.parametrize(
    ("database_time", "expected_state"),
    [
        (OPEN_AT - timedelta(microseconds=1), RoundState.SCHEDULED),
        (OPEN_AT, RoundState.OPEN),
        (OPEN_AT + timedelta(seconds=10), RoundState.AWAITING_VALIDATION),
    ],
)
def test_round_clock_boundaries(database_time: datetime, expected_state: RoundState) -> None:
    assert resolve_timed_round_state(_round(), database_time) is expected_state


def test_clock_derived_states_have_monotonic_versions_within_persisted_version() -> None:
    versions = [
        round_state_version(4, state)
        for state in (
            RoundState.SCHEDULED,
            RoundState.OPEN,
            RoundState.AWAITING_VALIDATION,
        )
    ]

    assert versions == sorted(versions)
    assert len(set(versions)) == 3
    assert round_state_version(5, RoundState.SCHEDULED) > versions[-1]


def test_round_state_version_derives_phase_width_from_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(CLOCK_PHASE_BY_STATE, RoundState.REVEAL, 4)

    assert round_state_version(5, RoundState.SCHEDULED) > round_state_version(
        4,
        RoundState.REVEAL,
    )


def test_standard_answer_window_is_half_open() -> None:
    round_ = _round(RoundState.OPEN)

    validate_answer_window(round_, OPEN_AT)
    validate_answer_window(round_, OPEN_AT + timedelta(milliseconds=9_999))
    with pytest.raises(AnswerWindowClosedError):
        validate_answer_window(round_, OPEN_AT + timedelta(seconds=10))


def test_legacy_result_processing_waits_for_answer_close() -> None:
    round_ = _round(RoundState.AWAITING_VALIDATION)

    with pytest.raises(PublicationTooEarlyError):
        validate_publication_time(round_, OPEN_AT + timedelta(milliseconds=9_999))
    validate_publication_time(round_, OPEN_AT + timedelta(seconds=10))


@pytest.mark.parametrize("invalid_elapsed", [True, False, 1.5, "1000", None, -1])
def test_client_timing_rejects_non_integer_or_negative_time(invalid_elapsed: object) -> None:
    with pytest.raises(AnswerWindowClosedError, match="client_elapsed_outside_selection_window"):
        validate_answer_window(
            _round(),
            OPEN_AT + timedelta(seconds=1),
            CLIENT_TIMING,
            cast(int, invalid_elapsed),
        )


def test_difference_uses_integer_ms_and_deadline_keeps_datetime_precision() -> None:
    validate_answer_window(
        _round(),
        OPEN_AT + timedelta(microseconds=1_000_999),
        CLIENT_TIMING,
        3500,
    )
    with pytest.raises(AnswerWindowClosedError, match="receipt_difference_exceeded"):
        validate_answer_window(
            _round(),
            OPEN_AT + timedelta(microseconds=999_999),
            CLIENT_TIMING,
            3500,
        )
    validate_answer_window(
        _round(),
        OPEN_AT + timedelta(microseconds=13_999_999),
        CLIENT_TIMING,
        0,
    )
    with pytest.raises(AnswerWindowClosedError, match="database_time_window"):
        validate_answer_window(
            _round(),
            OPEN_AT + timedelta(milliseconds=14_000),
            CLIENT_TIMING,
            0,
        )


def test_client_timing_publication_waits_until_fourteen_seconds() -> None:
    with pytest.raises(PublicationTooEarlyError):
        validate_publication_time(
            _round(), OPEN_AT + timedelta(microseconds=13_999_999), CLIENT_TIMING
        )
    validate_publication_time(_round(), OPEN_AT + timedelta(milliseconds=14_000), CLIENT_TIMING)


@pytest.mark.parametrize("difference_ms", [-2501, -2500, -2499])
def test_client_ahead_difference_boundaries(difference_ms: int) -> None:
    elapsed_ms = 3500
    received_at = OPEN_AT + timedelta(milliseconds=elapsed_ms + difference_ms)
    if difference_ms >= -2500:
        validate_answer_window(_round(), received_at, CLIENT_TIMING, elapsed_ms)
    else:
        with pytest.raises(AnswerWindowClosedError, match="receipt_difference_exceeded"):
            validate_answer_window(_round(), received_at, CLIENT_TIMING, elapsed_ms)


def test_positive_difference_configures_fourteen_second_upper_bound() -> None:
    round_ = _round()
    round_.standard_close_at = OPEN_AT + timedelta(seconds=20)

    validate_answer_window(round_, OPEN_AT + timedelta(milliseconds=14_000), CLIENT_TIMING, 0)
    with pytest.raises(AnswerWindowClosedError, match="receipt_difference_exceeded"):
        validate_answer_window(round_, OPEN_AT + timedelta(milliseconds=14_001), CLIENT_TIMING, 0)


def test_database_arrival_deadline_is_fourteen_seconds() -> None:
    validate_answer_window(_round(), OPEN_AT + timedelta(milliseconds=13_999), CLIENT_TIMING, 0)
    with pytest.raises(AnswerWindowClosedError, match="database_time_window"):
        validate_answer_window(_round(), OPEN_AT + timedelta(milliseconds=14_000), CLIENT_TIMING, 0)


def test_saved_client_policy_keeps_its_previous_ahead_tolerance() -> None:
    previous_timing = CLIENT_TIMING.model_copy(
        update={"client_ahead_tolerance_ms": 250, "clock_tolerance_ms": 250}
    )
    with pytest.raises(AnswerWindowClosedError, match="receipt_difference_exceeded"):
        validate_answer_window(
            _round(), OPEN_AT + timedelta(milliseconds=749), previous_timing, 1000
        )
    validate_answer_window(_round(), OPEN_AT + timedelta(milliseconds=750), previous_timing, 1000)
