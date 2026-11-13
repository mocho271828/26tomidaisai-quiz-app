from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from quiz_app.operator.control_contracts import OperatorMutationRequest


class FinalRoundMutationRequest(OperatorMutationRequest):
    round_id: UUID
    is_final: StrictBool


class FinalRoundResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    event_version: StrictInt = Field(ge=1)
    final_round_id: UUID | None
