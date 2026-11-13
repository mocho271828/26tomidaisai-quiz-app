"""SSMの生出力を捨て、既知のhost失敗診断だけを再検証して表示する。"""

import json
import sys
from typing import Literal

import logfire
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from validation.aws_review.diagnostics import (
    _AWS_ERROR_CODES,
    _ERROR_TYPES,
    _HOST_AWS_OPERATIONS,
)

_HOST_MODULES = frozenset(
    {
        "validation/aws_review/host.py",
        "validation/aws_review/host_update.py",
        "validation/aws_review/host_reset.py",
        "validation/aws_review/event_status_host.py",
        "validation/aws_review/reset_data.py",
        "validation/aws_review/reset_archive.py",
        "validation/aws_review/monitoring_install.py",
        "validation/aws_review/host_contracts.py",
        "validation/aws_review/host_data.py",
        "validation/aws_review/participant_ids.py",
        "validation/aws_review/diagnostics.py",
        "validation/aws_demo_deployment.py",
        "validation/rehearsal.py",
        "backend/src/quiz_app/aws_demo_paths.py",
        "backend/src/quiz_app/dependency_hosts.py",
        "backend/src/quiz_app/rehearsal/initialization.py",
        "backend/src/quiz_app/rehearsal/preparation.py",
    }
)


class _Location(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    module: str
    line: int = Field(ge=1, le=100_000)


class _HostFailure(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    status: Literal["failed"]
    code: Literal["review_host_failed"]
    error_type: str
    locations: tuple[_Location, ...] = Field(max_length=12)
    aws_operation: str | None = None
    aws_code: str | None = None

    @field_validator("error_type")
    @classmethod
    def known_error_type(cls, value: str) -> str:
        return value if value in _ERROR_TYPES else "Error"

    @field_validator("locations")
    @classmethod
    def known_locations(cls, value: tuple[_Location, ...]) -> tuple[_Location, ...]:
        # 相対pathの書式だけでは、秘密を含む任意filenameまで公開してしまう。
        return tuple(location for location in value if location.module in _HOST_MODULES)

    @field_validator("aws_operation")
    @classmethod
    def known_operation(cls, value: str | None) -> str | None:
        return value if value in _HOST_AWS_OPERATIONS else None

    @field_validator("aws_code")
    @classmethod
    def known_aws_code(cls, value: str | None) -> str | None:
        return value if value in _AWS_ERROR_CODES else None


def report_host_failure(standard_error: str) -> None:
    """最後の有効なhost診断だけを表示し、不正・欠測でも配置失敗の判定を維持する。"""
    for line in reversed(standard_error.splitlines()):
        try:
            failure = _HostFailure.model_validate_json(line)
        except ValidationError:
            # installerや子processの出力、切り詰められたJSONを例外本文ごと捨てる。
            continue
        details = failure.model_dump(mode="json", exclude_none=True)
        logfire.error("AWS review host execution failed", **details)
        try:
            print(json.dumps(details, ensure_ascii=False), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            logfire.warning("AWS review host diagnostic output unavailable")
        return
