from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from quiz_app.answer_timing import AnswerTimingBasis
from quiz_app.models.enums import EventState, RoundState, SpeedCohort
from quiz_app.public_label import PublicLabel
from quiz_app.quiz_configuration import ResultVisibility
from quiz_app.quiz_configuration.contracts import AwardCeremonyPhase


class ParticipantJoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_join_token: StrictStr = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
        description=(
            "初回応答消失時の短時間再試行だけに使うclient生成256bit secret。表示IDとは別に扱う。"
        ),
    )


class AudienceEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_title: str
    event_state: EventState
    event_version: int = Field(ge=1)
    start_delay_seconds: int = Field(ge=1)
    timing_basis: AnswerTimingBasis
    arrival_grace_ms: StrictInt = Field(ge=0)
    clock_tolerance_ms: StrictInt = Field(ge=0)
    client_ahead_tolerance_ms: StrictInt = Field(ge=0)


class ParticipantIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_label: PublicLabel
    is_ranking_eligible: StrictBool


class AudienceOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: UUID
    label: str
    content: str


class AudienceQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    prompt: str
    options: list[AudienceOptionResponse]


class PublishedOptionResponse(AudienceOptionResponse):
    is_correct: bool
    valid_response_count: int = Field(ge=0)
    response_percentage: float = Field(ge=0, le=100)


class PublishedDistributionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid_answer_count: int = Field(ge=0)
    options: list[PublishedOptionResponse]


class PublishedResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication: Literal["Published"]
    correct_option_id: UUID
    distribution: PublishedDistributionResponse | None


class ScoredParticipantOutcomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["Scored"]
    is_correct: StrictBool
    base_points: StrictInt = Field(ge=0, description="保存済み採点台帳の正答点。")
    speed_points: StrictInt = Field(ge=0, description="保存済み採点台帳の速度点。")
    total_points: StrictInt = Field(ge=0)
    elapsed_ms: StrictInt | None = Field(
        ge=0,
        description="timing_basisに従う保存済み正式回答時間（ms）。旧記録の欠損はnull。",
    )
    timing_basis: AnswerTimingBasis
    scoring_policy_version: StrictStr = Field(min_length=1, max_length=64)
    speed_cohort: SpeedCohort
    speed_rank: StrictInt | None = Field(ge=1)
    speed_cohort_size: StrictInt | None = Field(ge=1)

    @model_validator(mode="after")
    def check_score_breakdown(self) -> "ScoredParticipantOutcomeResponse":
        if self.timing_basis == "client_monotonic_v1" and self.elapsed_ms is None:
            raise ValueError("client_elapsed_ms_missing")
        if self.total_points != self.base_points + self.speed_points:
            raise ValueError("participant_score_breakdown_mismatch")
        return self


class UnansweredParticipantOutcomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["Unanswered"]


ParticipantOutcomeResponse = Annotated[
    ScoredParticipantOutcomeResponse | UnansweredParticipantOutcomeResponse,
    Field(discriminator="status"),
]


class RoundTimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_id: UUID
    round_number: StrictInt = Field(ge=1, description="開始前VOIDを除く開催内の1始まり出題番号。")
    version: int = Field(ge=1)
    state_version: int = Field(ge=1)
    state: RoundState
    open_at: AwareDatetime | None
    standard_close_at: AwareDatetime | None


class ParticipantCurrentRoundResponse(RoundTimelineResponse):
    question: AudienceQuestionResponse | SkipJsonSchema[None] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ParticipantCommonLatestResultResponse(RoundTimelineResponse):
    """開催内で共有できる公開結果だけを持つ。"""

    question: AudienceQuestionResponse
    result: PublishedResultResponse


class PersonalResultRevision(BaseModel):
    """公開境界を既存Event版と確定済み結果の版に結び付ける。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    event_version: StrictInt = Field(ge=1)
    result_round_id: UUID | None
    result_round_version: StrictInt | None = Field(ge=1)
    ranking_snapshot_version: StrictInt | None = Field(ge=1)
    result_visibility: ResultVisibility

    @model_validator(mode="after")
    def check_result_round(self) -> "PersonalResultRevision":
        if (self.result_round_id is None) != (self.result_round_version is None):
            raise ValueError("personal_result_round_revision_mismatch")
        return self


class PersonalResultDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["none", "pending", "ready", "unavailable"]
    revision: PersonalResultRevision | None

    @model_validator(mode="after")
    def check_revision(self) -> "PersonalResultDelivery":
        if self.status in {"pending", "ready"} and self.revision is None:
            raise ValueError("personal_result_revision_required")
        if self.status == "none" and self.revision is not None:
            raise ValueError("personal_result_revision_not_expected")
        return self


class AwardCeremonyRankingRowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    public_label: PublicLabel
    overall_rank: StrictInt = Field(ge=1, le=5)


class AwardCeremonyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: AwardCeremonyPhase
    top_rows: list[AwardCeremonyRankingRowResponse] | None

    @model_validator(mode="after")
    def check_publication(self) -> "AwardCeremonyResponse":
        if (self.phase == "Announced") != (self.top_rows is not None):
            raise ValueError("award_ceremony_rows_publication_mismatch")
        return self


class ParticipantCommonStateResponse(BaseModel):
    """本人専用の回答・得点・順位を含まない参加者用snapshot。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projected_at: AwareDatetime
    event: AudienceEventResponse
    award_ceremony: AwardCeremonyResponse | None
    current_round: ParticipantCurrentRoundResponse | None
    latest_result: ParticipantCommonLatestResultResponse | None
    result_visibility: ResultVisibility
    latest_ranking_snapshot_version: StrictInt | None = Field(ge=1)
    personal_result_delivery: PersonalResultDelivery = Field(
        default_factory=lambda: PersonalResultDelivery(status="none", revision=None)
    )

    @model_validator(mode="after")
    def check_award_ceremony(self) -> "ParticipantCommonStateResponse":
        if (self.event.event_state is EventState.COMPLETED) != (self.award_ceremony is not None):
            raise ValueError("award_ceremony_event_state_mismatch")
        return self


class ParticipantAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_id: UUID
    round_id: UUID
    option_id: UUID
    client_answer_id: UUID
    received_at: AwareDatetime
    client_elapsed_ms: StrictInt | None = Field(ge=0)
    timing_basis: AnswerTimingBasis

    @model_validator(mode="after")
    def check_timing_basis(self) -> "ParticipantAnswerResponse":
        if self.timing_basis == "client_monotonic_v1" and self.client_elapsed_ms is None:
            raise ValueError("client_elapsed_ms_missing")
        return self


class AcceptedAnswerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["AcceptedLocked"]
    answer: ParticipantAnswerResponse


class UnsavedAnswerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["NotStored"]
    client_answer_id: UUID


AnswerStatusResponse = Annotated[
    AcceptedAnswerStatusResponse | UnsavedAnswerStatusResponse,
    Field(discriminator="status"),
]


class RankingRowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_label: PublicLabel
    overall_rank: int = Field(ge=1)
    total_points: int = Field(ge=0)
    correct_count: int = Field(ge=0)


class ParticipantTopRankingRowResponse(RankingRowResponse):
    overall_rank: int = Field(ge=1, le=3)


class ParticipantRankingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: int = Field(ge=1)
    overall_rank: int = Field(ge=1)
    total_points: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    top_rows: list[ParticipantTopRankingRowResponse] | None


class PersonalResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_option_id: UUID | None
    outcome: ParticipantOutcomeResponse | None
    ranking: ParticipantRankingResponse | None


class PersonalResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery: PersonalResultDelivery
    result: PersonalResultPayload | None

    @model_validator(mode="after")
    def check_payload(self) -> "PersonalResultResponse":
        if (self.delivery.status == "ready") != (self.result is not None):
            raise ValueError("personal_result_readiness_mismatch")
        revision = self.delivery.revision
        if self.result is not None and revision is not None:
            if (revision.result_round_id is None) != (self.result.outcome is None):
                raise ValueError("personal_result_outcome_mismatch")
            outcome, ranking = self.result.outcome, self.result.ranking
            if revision.result_round_id is None and self.result.selected_option_id is not None:
                raise ValueError("personal_result_selection_without_round")
            if not revision.result_visibility.personal_ranking and (
                ranking is not None
                or (
                    isinstance(outcome, ScoredParticipantOutcomeResponse)
                    and (outcome.speed_rank is not None or outcome.speed_cohort_size is not None)
                )
            ):
                raise ValueError("personal_result_ranking_not_public")
            if ranking is not None:
                if ranking.snapshot_version != revision.ranking_snapshot_version:
                    raise ValueError("personal_result_ranking_revision_mismatch")
                if not revision.result_visibility.overall_ranking and ranking.top_rows is not None:
                    raise ValueError("personal_result_top_rows_not_public")
        return self


class ParticipantStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projected_at: AwareDatetime
    event: AudienceEventResponse
    award_ceremony: AwardCeremonyResponse | None
    participant: ParticipantIdentityResponse
    current_round: ParticipantCurrentRoundResponse | None
    latest_result: ParticipantCommonLatestResultResponse | None
    result_visibility: ResultVisibility
    latest_ranking_snapshot_version: StrictInt | None = Field(ge=1)
    personal_result_delivery: PersonalResultDelivery
    answer: ParticipantAnswerResponse | SkipJsonSchema[None] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def check_award_ceremony(self) -> "ParticipantStateResponse":
        if (self.event.event_state is EventState.COMPLETED) != (self.award_ceremony is not None):
            raise ValueError("award_ceremony_event_state_mismatch")
        return self


class SubmitAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_version: StrictInt = Field(ge=1)
    option_id: UUID
    client_answer_id: UUID
    client_elapsed_ms: StrictInt = Field(ge=0, lt=10_000)


class AnswerReceiptResponse(ParticipantAnswerResponse):
    is_replay: bool


class TimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_time: AwareDatetime
    shared_sample_age_ms: float = Field(ge=0, allow_inf_nan=False)
