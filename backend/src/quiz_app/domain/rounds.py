from datetime import datetime, timedelta

from quiz_app.answer_timing import LEGACY_ANSWER_TIMING, AnswerTiming
from quiz_app.domain.errors import (
    AnswerWindowClosedError,
    ClientReceiptDifferenceError,
    ClientSelectionTimeError,
    PublicationTooEarlyError,
)
from quiz_app.models.enums import RoundState
from quiz_app.models.quiz import QuizRound

STANDARD_DURATION_SECONDS = 10
STANDARD_DURATION_MS = STANDARD_DURATION_SECONDS * 1_000
CLOCK_PHASE_BY_STATE = {
    RoundState.SCHEDULED: 0,
    RoundState.OPEN: 1,
    RoundState.AWAITING_VALIDATION: 2,
}


def round_state_version(round_version: int, state: RoundState) -> int:
    """永続version内の時刻導出stateにも単調な表示順序を与える。"""

    if round_version < 1:
        raise ValueError("round_version_must_be_positive")
    phase_width = max(CLOCK_PHASE_BY_STATE.values(), default=0) + 1
    return round_version * phase_width + CLOCK_PHASE_BY_STATE.get(state, 0)


def resolve_timed_round_state(round_: QuizRound, database_time: datetime) -> RoundState:
    """DB時刻だけを使い、時間により到達すべきRound状態を求める。"""

    if round_.open_at is None or round_.standard_close_at is None:
        return round_.state
    if round_.state not in {
        RoundState.SCHEDULED,
        RoundState.OPEN,
    }:
        return round_.state
    if database_time >= round_.standard_close_at:
        return RoundState.AWAITING_VALIDATION
    if database_time >= round_.open_at:
        return RoundState.OPEN
    return RoundState.SCHEDULED


def validate_answer_window(
    round_: QuizRound,
    received_at: datetime,
    timing: AnswerTiming = LEGACY_ANSWER_TIMING,
    client_elapsed_ms: int | None = None,
) -> None:
    """PostgreSQLが生成した受付時刻を半開区間で検証する。"""

    if round_.open_at is None:
        raise AnswerWindowClosedError("round_not_scheduled")
    effective_close_at = round_.standard_close_at
    if effective_close_at is None:
        raise AnswerWindowClosedError("round_close_not_defined")
    arrival_close_at = effective_close_at + timedelta(milliseconds=timing.arrival_grace_ms)
    if received_at < round_.open_at or received_at >= arrival_close_at:
        raise AnswerWindowClosedError("answer_outside_database_time_window")
    if timing.basis == "server_received_v1":
        return
    selection_duration_ms = (effective_close_at - round_.open_at) // timedelta(milliseconds=1)
    if type(client_elapsed_ms) is not int or not 0 <= client_elapsed_ms < selection_duration_ms:
        raise ClientSelectionTimeError("client_elapsed_outside_selection_window")
    received_elapsed_ms = (received_at - round_.open_at) // timedelta(milliseconds=1)
    difference_ms = received_elapsed_ms - client_elapsed_ms
    if not (
        -timing.client_ahead_tolerance_ms
        <= difference_ms
        <= timing.arrival_grace_ms + timing.clock_tolerance_ms
    ):
        raise ClientReceiptDifferenceError("client_elapsed_receipt_difference_exceeded")


def validate_publication_time(
    round_: QuizRound,
    database_time: datetime,
    timing: AnswerTiming = LEGACY_ANSWER_TIMING,
) -> None:
    """全回答の到着猶予が終わるまで結果処理を拒否する。"""

    if round_.standard_close_at is None or database_time < (
        round_.standard_close_at + timedelta(milliseconds=timing.arrival_grace_ms)
    ):
        raise PublicationTooEarlyError("answer_arrival_deadline_not_reached")
