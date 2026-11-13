from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quiz_app.answer_timing import AnswerTiming

VisibilityTarget = Literal["distribution", "personal_ranking", "overall_ranking"]
AwardCeremonyPhase = Literal["Waiting", "Announced", "Finished"]
VisibilityMutationTarget = VisibilityTarget | Literal["rankings"]
VISIBILITY_TARGETS: tuple[VisibilityTarget, ...] = (
    "distribution",
    "personal_ranking",
    "overall_ranking",
)


class ConfigurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class VisibilityChange(ConfigurationModel):
    after_round: int = Field(ge=1)
    visible: bool


class VisibilityControl(ConfigurationModel):
    mode: Literal["manual", "automatic"]
    initially_visible: bool
    changes: tuple[VisibilityChange, ...]

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        if self.mode == "manual" and self.changes:
            raise ValueError("manual_visibility_must_not_have_changes")
        if self.mode == "automatic" and not self.changes:
            raise ValueError("automatic_visibility_requires_changes")
        rounds = [change.after_round for change in self.changes]
        if any(
            current >= following for current, following in zip(rounds, rounds[1:], strict=False)
        ):
            raise ValueError("visibility_changes_must_be_strictly_ordered")
        return self


class VisibilityControls(ConfigurationModel):
    distribution: VisibilityControl
    personal_ranking: VisibilityControl
    overall_ranking: VisibilityControl


class ResultVisibility(ConfigurationModel):
    distribution: bool
    personal_ranking: bool
    overall_ranking: bool


class ScoringConfiguration(ConfigurationModel):
    version: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    correct_points: int = Field(ge=0, le=1_000_000)
    maximum_speed_bonus: int = Field(ge=0, le=1_000_000)
    seconds_per_point: float = Field(gt=0, le=10, allow_inf_nan=False)

    @field_validator("seconds_per_point")
    @classmethod
    def validate_millisecond_precision(cls, value: float) -> float:
        milliseconds = Decimal(str(value)) * 1000
        if milliseconds != milliseconds.to_integral_value():
            raise ValueError("seconds_per_point_requires_whole_milliseconds")
        return value

    @property
    def speed_bucket_ms(self) -> int:
        return int(Decimal(str(self.seconds_per_point)) * 1000)


class QuizSettings(ConfigurationModel):
    schema_version: int = Field(ge=3, le=3)
    version: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    start_delay_seconds: int = Field(ge=1, le=300)
    visibility: VisibilityControls
    scoring: ScoringConfiguration
    timing: AnswerTiming
