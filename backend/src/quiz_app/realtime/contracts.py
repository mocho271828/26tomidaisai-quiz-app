from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI
from pydantic import UUID4, BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from quiz_app.audience.contracts import (
    ParticipantCommonStateResponse,
    PersonalResultDelivery,
    PersonalResultResponse,
)
from quiz_app.operator.control_contracts import OperatorStateResponse

RealtimeAudience = Literal["participant", "operator"]
InvalidationCause = Literal[
    "participant_join", "participant_answer", "operator_mutation", "automatic_result"
]


class StateInvalidatedMessage(BaseModel):
    """Redis内部で共通projectionの再構築を通知する。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["state.invalidated"]
    audience: RealtimeAudience
    event_id: UUID
    cause: InvalidationCause = "operator_mutation"


class StreamMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audience: RealtimeAudience
    event_id: UUID
    stream_id: UUID4
    sequence: StrictInt = Field(ge=1)


class ParticipantStateMessage(StreamMessage):
    type: Literal["participant.state"]
    audience: Literal["participant"]
    state: ParticipantCommonStateResponse


class OperatorStateMessage(StreamMessage):
    type: Literal["operator.state"]
    audience: Literal["operator"]
    state: OperatorStateResponse


class ParticipantResultMessage(StreamMessage):
    type: Literal["participant.result"]
    audience: Literal["participant"]
    result: PersonalResultResponse

    @model_validator(mode="after")
    def check_event(self) -> "ParticipantResultMessage":
        revision = self.result.delivery.revision
        if revision is not None and revision.event_id != self.event_id:
            raise ValueError("personal_result_event_mismatch")
        return self


class HeartbeatMessage(StreamMessage):
    type: Literal["heartbeat"]
    personal_result_delivery: PersonalResultDelivery | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def check_audience(self) -> "HeartbeatMessage":
        if self.audience != "participant" and self.personal_result_delivery is not None:
            raise ValueError("personal_result_delivery_requires_participant")
        if self.personal_result_delivery is not None:
            revision = self.personal_result_delivery.revision
            if revision is not None and revision.event_id != self.event_id:
                raise ValueError("personal_result_event_mismatch")
        return self


class SessionChangedMessage(StreamMessage):
    """有効なsessionの属性変更後に本人projectionを再取得させる。"""

    type: Literal["session.changed"]


class PollingRequiredMessage(BaseModel):
    """WebSocketを使えない場合の最短5秒のpolling契約。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["polling.required"]
    reason: Literal["realtime_unavailable"]
    poll_after_ms: StrictInt = Field(ge=5_000, le=5_000)


RealtimeServerMessage = Annotated[
    ParticipantStateMessage
    | ParticipantResultMessage
    | OperatorStateMessage
    | HeartbeatMessage
    | SessionChangedMessage
    | PollingRequiredMessage,
    Field(discriminator="type"),
]


def install_realtime_openapi(application: FastAPI) -> None:
    """HTTP routeを増やさず、WS契約も生成OpenAPIへ登録する。"""

    original_openapi = application.openapi

    def openapi_with_realtime() -> dict[str, object]:
        schema = original_openapi()
        message_schema = TypeAdapter(RealtimeServerMessage).json_schema(
            ref_template="#/components/schemas/{model}"
        )
        definitions = message_schema.pop("$defs", {})
        schema.setdefault("components", {}).setdefault("schemas", {}).update(definitions)
        schema["components"]["schemas"]["RealtimeServerMessage"] = message_schema
        schema["x-websockets"] = {
            path: {
                "audience": audience,
                "message": {"$ref": "#/components/schemas/RealtimeServerMessage"},
            }
            for path, audience in (
                ("/api/v1/participant/realtime", "participant"),
                ("/api/v1/operator/realtime", "operator"),
            )
        }
        return schema

    application.openapi = openapi_with_realtime
