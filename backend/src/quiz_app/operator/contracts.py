from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from quiz_app.models.enums import AdminRole

NormalizedUsername = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


class OperatorLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: NormalizedUsername
    password: SecretStr = Field(min_length=1, max_length=256)


class OperatorIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: UUID
    username: str
    role: AdminRole


class OperatorSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentityResponse
