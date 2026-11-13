"""readinessを前提とする旧600VU/10問oracle専用の応答契約。

2026-09-30 P0 APIにgateがない場合は必須field検証で拒否する。
現在のAPIへ互換応答を追加したり、旧oracleを緩めたりしない。
"""

from pydantic import BaseModel, ConfigDict

from quiz_app.operator.control_contracts import (
    OperatorRoundResponse as CurrentOperatorRoundResponse,
)
from quiz_app.operator.control_contracts import (
    OperatorStateResponse as CurrentOperatorStateResponse,
)


class OperatorGateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_count: int
    ready_count: int
    asset_failure_count: int
    can_schedule: bool
    blocking_reasons: list[str]


class OperatorRoundResponse(CurrentOperatorRoundResponse):
    gate: OperatorGateResponse


class OperatorStateResponse(CurrentOperatorStateResponse):
    current_round: OperatorRoundResponse | None
