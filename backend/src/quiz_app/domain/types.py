from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from quiz_app.answer_timing import AnswerTimingBasis
from quiz_app.models.enums import RoundState, ScoreExclusionReason, SpeedCohort

if TYPE_CHECKING:
    from quiz_app.quiz_configuration import ResultVisibility


@dataclass(frozen=True, slots=True)
class ScoringRule:
    version: str
    base_points: int
    initial_speed_bonus: int
    speed_bucket_ms: int
    speed_decrement_per_bucket: int
    standard_duration_ms: int
    timing_basis: AnswerTimingBasis = "server_received_v1"
    arrival_grace_ms: int = 0
    clock_tolerance_ms: int = 0
    client_ahead_tolerance_ms: int = 0


@dataclass(frozen=True, slots=True)
class AnswerScoreInput:
    participant_id: UUID
    public_label: str
    is_correct: bool
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ScoredAnswer:
    participant_id: UUID
    public_label: str
    is_correct: bool
    base_points: int
    speed_points: int
    total_points: int
    elapsed_ms: int
    speed_bucket: int
    speed_cohort: SpeedCohort
    exclusion_reason: ScoreExclusionReason
    speed_rank: int | None = None
    speed_cohort_size: int | None = None


@dataclass(frozen=True, slots=True)
class OverallStandingInput:
    participant_id: UUID
    public_label: str
    total_points: int
    correct_count: int
    correct_elapsed_ms: int


@dataclass(frozen=True, slots=True)
class OverallStanding:
    participant_id: UUID
    public_label: str
    overall_rank: int
    total_points: int
    correct_count: int


@dataclass(frozen=True, slots=True)
class SubmitAnswerCommand:
    participant_id: UUID
    round_id: UUID
    round_version: int
    option_id: UUID
    client_answer_id: UUID
    client_elapsed_ms: int


@dataclass(frozen=True, slots=True)
class AcceptedAnswer:
    answer_id: UUID
    round_id: UUID
    option_id: UUID
    client_answer_id: UUID
    received_at: datetime
    is_replay: bool
    client_elapsed_ms: int | None
    timing_basis: AnswerTimingBasis


@dataclass(frozen=True, slots=True)
class ScheduledRound:
    round_id: UUID
    version: int
    open_at: datetime
    standard_close_at: datetime


@dataclass(frozen=True, slots=True)
class PreparationStarted:
    round_id: UUID
    round_version: int
    round_state: RoundState


@dataclass(frozen=True, slots=True)
class SelectedRoundQuestion:
    event_id: UUID
    event_version: int
    round_id: UUID
    round_version: int
    round_state: RoundState
    question_id: UUID


@dataclass(frozen=True, slots=True)
class RoundClockState:
    round_id: UUID
    round_version: int
    round_state: RoundState
    event_version: int


@dataclass(frozen=True, slots=True)
class AggregatedRound:
    event_id: UUID
    round_id: UUID
    round_version: int
    event_version: int
    valid_answer_count: int
    ranking_row_count: int


@dataclass(frozen=True, slots=True)
class PublishedRound:
    event_id: UUID
    round_id: UUID
    round_version: int
    event_version: int
    valid_answer_count: int
    ranking_row_count: int


@dataclass(frozen=True, slots=True)
class RecoveredRoundResults:
    event_id: UUID
    event_version: int
    round_id: UUID
    round_version: int
    round_state: RoundState


@dataclass(frozen=True, slots=True)
class RoundBackupTransition:
    event_id: UUID
    round_id: UUID
    round_version: int
    round_state: RoundState


@dataclass(frozen=True, slots=True)
class AdvancedRound:
    event_id: UUID
    event_version: int
    previous_round_id: UUID
    new_round_id: UUID
    new_round_version: int
    new_round_state: RoundState


@dataclass(frozen=True, slots=True)
class CompletedEvent:
    event_id: UUID
    event_version: int
    round_id: UUID
    round_version: int
    round_state: RoundState


@dataclass(frozen=True, slots=True)
class VisibilityChanged:
    event_id: UUID
    event_version: int
    visibility: ResultVisibility


@dataclass(slots=True)
class StandingAccumulator:
    participant_id: UUID
    public_label: str
    total_points: int = 0
    correct_count: int = 0
    correct_elapsed_ms: int = 0
