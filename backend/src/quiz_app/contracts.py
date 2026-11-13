from typing import Literal

from pydantic import BaseModel, ConfigDict

RedisDependencyStatus = Literal["ok", "degraded"]


class LiveHealthResponse(BaseModel):
    """dependencyを参照しないliveness contract。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: Literal["quiz-api"] = "quiz-api"
    version: str


class ReadinessHealthResponse(BaseModel):
    """DB schema適用状態を外部詳細なしで返すreadiness contract。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded", "not_ready"]
    database: Literal["ok", "unavailable"]
    migration: Literal["current", "missing", "outdated", "unknown"]
    redis: RedisDependencyStatus


class ApiErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
