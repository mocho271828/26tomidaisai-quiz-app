from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

from quiz_app.models.enums import (
    EventState,
    RoundResultPublication,
    RoundState,
)
from quiz_app.public_label import PublicLabel
from quiz_app.quiz_configuration import (
    ResultVisibility,
    VisibilityControls,
    VisibilityMutationTarget,
)
from quiz_app.quiz_configuration.contracts import AwardCeremonyPhase


class OperatorMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    idempotency_key: UUID
    execute_not_after: AwareDatetime | None = None


class ScheduleRoundMutationRequest(OperatorMutationRequest):
    target_open_at: AwareDatetime | None = None


class ResultVisibilityMutationRequest(OperatorMutationRequest):
    target: VisibilityMutationTarget
    visible: StrictBool


class AwardCeremonyMutationRequest(OperatorMutationRequest):
    phase: Literal["Announced", "Finished"]


class QuestionSelectionMutationRequest(OperatorMutationRequest):
    question_id: UUID


class NextRoundMutationRequest(OperatorMutationRequest):
    question_id: UUID


class OperatorOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: UUID
    label: str
    content: str
    is_correct: bool


class OperatorQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    version: int
    prompt: str
    options: list[OperatorOptionResponse]

    @model_validator(mode="after")
    def validate_unique_option_ids(self) -> Self:
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("question option_id must be unique")
        return self


class OperatorQuestionPreviewResponse(OperatorQuestionResponse):
    display_order: int


class OperatorOptionStatisticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: UUID
    answer_count: int = Field(ge=0)
    average_response_ms: float | None = Field(ge=0)

    @model_validator(mode="after")
    def validate_average_presence(self) -> Self:
        if (self.answer_count == 0) != (self.average_response_ms is None):
            raise ValueError("option average_response_ms must be null exactly when count is zero")
        return self


class OperatorRoundStatisticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_count: int = Field(ge=0)
    average_response_ms: float | None = Field(ge=0)
    options: list[OperatorOptionStatisticsResponse]

    @model_validator(mode="after")
    def validate_totals_and_option_ids(self) -> Self:
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("statistics option_id must be unique")
        if self.answer_count != sum(option.answer_count for option in self.options):
            raise ValueError("answer_count must equal the sum of option answer counts")
        if (self.answer_count == 0) != (self.average_response_ms is None):
            raise ValueError("average_response_ms must be null exactly when total count is zero")
        return self


class OperatorRoundResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_id: UUID
    version: int
    state_version: int
    state: RoundState
    open_at: datetime | None
    standard_close_at: datetime | None
    result_publication: RoundResultPublication
    can_publish_results: bool
    result_failure_code: (
        Annotated[
            str,
            StringConstraints(
                pattern=(
                    r"^automatic_aggregate_(?:integrity_failure|domain_failure"
                    r"(?::[a-z][a-z0-9_]{0,79})?)$"
                ),
            ),
        ]
        | None
    )
    statistics: OperatorRoundStatisticsResponse
    question: OperatorQuestionResponse

    @model_validator(mode="after")
    def validate_statistics_option_ids(self) -> Self:
        question_option_ids = {option.option_id for option in self.question.options}
        statistics_option_ids = {option.option_id for option in self.statistics.options}
        if question_option_ids != statistics_option_ids:
            raise ValueError("question and statistics option_id sets must match")
        if (self.state is RoundState.VOID_CANDIDATE) != (self.result_failure_code is not None):
            raise ValueError("result_failure_code must be present exactly for VoidCandidate")
        return self


class OperatorRankingRowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_rank: int = Field(ge=1)
    public_label: PublicLabel
    total_points: int = Field(ge=0)
    correct_count: int = Field(ge=0)


class OperatorLatestRankingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: int = Field(ge=1)
    round_id: UUID
    rows: list[OperatorRankingRowResponse]


class OperatorStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    projected_at: datetime
    event_title: str
    event_state: EventState
    award_ceremony_phase: AwardCeremonyPhase | None
    event_version: int
    start_delay_seconds: int = Field(ge=1)
    result_visibility: ResultVisibility
    visibility_controls: VisibilityControls
    participant_count: int
    available_questions: list[OperatorQuestionPreviewResponse]
    used_question_ids: list[UUID]
    current_round: OperatorRoundResponse | None
    latest_ranking_snapshot_version: int | None = Field(ge=1)

    @model_validator(mode="after")
    def check_award_ceremony(self) -> "OperatorStateResponse":
        if (self.event_state is EventState.COMPLETED) != (self.award_ceremony_phase is not None):
            raise ValueError("award_ceremony_event_state_mismatch")
        return self


class OperatorMutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "begin_preparation",
        "select_round_question",
        "schedule_round",
        "retry_round_results",
        "publish_round_results",
        "advance_round",
        "complete_event",
        "set_result_visibility",
        "set_final_round",
        "set_award_ceremony",
    ]
    event_id: UUID
    event_version: int
    round_id: UUID | None = None
    round_version: int | None = None
    round_state: RoundState | None = None
    result_visibility: ResultVisibility
    open_at: datetime | None = None
    standard_close_at: datetime | None = None
    valid_answer_count: int | None = None
    ranking_row_count: int | None = None
    is_replay: bool = False
