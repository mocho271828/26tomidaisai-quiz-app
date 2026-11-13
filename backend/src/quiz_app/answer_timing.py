from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

AnswerTimingBasis = Literal["server_received_v1", "client_monotonic_v1"]


class AnswerTiming(BaseModel):
    """開催と採点policyへ固定する回答計測の契約。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)

    basis: AnswerTimingBasis
    arrival_grace_ms: int = Field(ge=0, le=60_000)
    clock_tolerance_ms: int = Field(ge=0, le=10_000)
    client_ahead_tolerance_ms: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_legacy_timing(self) -> Self:
        if self.basis == "server_received_v1" and (
            self.arrival_grace_ms != 0
            or self.clock_tolerance_ms != 0
            or self.client_ahead_tolerance_ms != 0
        ):
            raise ValueError("legacy_timing_requires_zero_tolerances")
        return self


LEGACY_ANSWER_TIMING = AnswerTiming(
    basis="server_received_v1",
    arrival_grace_ms=0,
    clock_tolerance_ms=0,
    client_ahead_tolerance_ms=0,
)
