from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quiz_app.answer_timing import AnswerTimingBasis
from quiz_app.public_label import PublicLabel


class OperatorResponseTimeFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_id: UUID | None = None


class OperatorResponseTimeRound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_id: UUID
    round_number: int = Field(ge=1)
    is_practice: bool
    prompt: str


class OperatorResponseTimeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    public_label: PublicLabel
    response_ms: int = Field(ge=0)
    is_correct: bool


class OperatorResponseTimeStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_count: int = Field(ge=0)
    average_response_ms: float | None = Field(ge=0)
    median_response_ms: float | None = Field(ge=0)
    fastest_response_ms: int | None = Field(ge=0)
    slowest_response_ms: int | None = Field(ge=0)
    rows: list[OperatorResponseTimeRow]

    @model_validator(mode="after")
    def validate_statistics(self) -> Self:
        if self.answer_count != len(self.rows):
            raise ValueError("answer_count must equal the number of rows")
        metrics = (
            self.average_response_ms,
            self.median_response_ms,
            self.fastest_response_ms,
            self.slowest_response_ms,
        )
        if any((self.answer_count == 0) != (value is None) for value in metrics):
            raise ValueError("response statistics must be null exactly when count is zero")
        return self


class OperatorResponseTimesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rounds: list[OperatorResponseTimeRound]
    selected_round_id: UUID | None
    timing_basis: AnswerTimingBasis
    all_answers: OperatorResponseTimeStatistics
    correct_answers: OperatorResponseTimeStatistics
