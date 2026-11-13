"""通知・WS障害を、資格情報を含む例外本文なしで記録する。"""

import re
from pathlib import Path
from typing import Literal

import logfire
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.domain.errors import DomainError
from quiz_app.realtime.contracts import RealtimeAudience

RealtimeFailureBoundary = Literal[
    "initial_snapshot",
    "state_or_authentication",
    "authentication_refresh",
    "state_send",
    "heartbeat_send",
    "session_change_send",
    "single_event",
    "shared_state",
    "answer_notification",
    "redis_publish",
    "redis_readiness",
    "redis_pubsub_close",
    "redis_client_close",
]
FailureKind = Literal["database", "redis", "timeout", "io", "domain", "realtime"]
_APP_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_MAX_FAILURE_FRAMES = 32


def preserve_realtime_failure_fields(match: logfire.ScrubMatch) -> str | None:
    """safe recordの固定公開語彙だけを、完全一致のpathでSDK秘匿から除外する。"""

    path = match.path
    if path[:2] not in {("message", "record"), ("attributes", "record")}:
        return None
    if path[2:] == ("boundary",):
        return (
            match.value
            if isinstance(match.value, str)
            and match.value
            in {
                "state_or_authentication",
                "authentication_refresh",
                "state_send",
                "heartbeat_send",
                "session_change_send",
            }
            else None
        )
    if len(path) != 5 or path[2] != "stack" or type(path[3]) is not int:
        return None
    if not 0 <= path[3] < _MAX_FAILURE_FRAMES:
        return None
    allowed = {
        "source_module": {
            "quiz_app.audience.auth_service",
            "quiz_app.audience.auth_cache",
            "quiz_app.operator.auth_service",
            "quiz_app.audience.async_service",
        },
        "source_function": {
            "_verify_in_session",
            "authenticate",
            "authenticate_realtime",
            "authenticate_in_session",
            "_participant_context",
            "get_participant_state_in_session",
            "get_answer_status_in_session",
        },
    }
    if isinstance(match.value, str) and match.value in allowed.get(path[4], set()):
        return match.value
    return None


class RealtimeFailureFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_module: str = Field(pattern=r"^quiz_app(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
    source_function: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    source_line: int = Field(gt=0)


class RealtimeFailureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: RealtimeFailureBoundary
    failure_kind: FailureKind
    audience: RealtimeAudience | None
    stack: list[RealtimeFailureFrame] = Field(max_length=_MAX_FAILURE_FRAMES)
    stack_state: Literal["available", "unavailable", "truncated"]


def _failure_kind(error: BaseException) -> FailureKind:
    if isinstance(error, SQLAlchemyError):
        return "database"
    if isinstance(error, RedisError):
        return "redis"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "io"
    if isinstance(error, DomainError):
        return "domain"
    return "realtime"


def realtime_failure_record(
    error: BaseException,
    boundary: RealtimeFailureBoundary,
    audience: RealtimeAudience | None,
) -> RealtimeFailureRecord:
    """既存回答診断と同じく、app内sourceのmodule・関数・行番号だけを採る。"""

    stack: list[RealtimeFailureFrame] = []
    stack_state: Literal["available", "unavailable", "truncated"] = "unavailable"
    traceback = error.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        line = traceback.tb_lineno
        traceback = traceback.tb_next
        try:
            relative = Path(code.co_filename).relative_to(_APP_SOURCE_ROOT)
        except ValueError:
            continue
        parts = (*relative.parts[:-1], relative.stem)
        if not all(_SOURCE_IDENTIFIER.fullmatch(part) for part in (*parts, code.co_name)):
            continue
        if len(stack) == _MAX_FAILURE_FRAMES:
            stack_state = "truncated"
            break
        stack.append(
            RealtimeFailureFrame(
                source_module=".".join(("quiz_app", *parts)),
                source_function=code.co_name,
                source_line=line,
            )
        )
        stack_state = "available"
    return RealtimeFailureRecord(
        boundary=boundary,
        failure_kind=_failure_kind(error),
        audience=audience,
        stack=stack,
        stack_state=stack_state,
    )


def log_realtime_failure(
    error: BaseException,
    boundary: RealtimeFailureBoundary,
    *,
    audience: RealtimeAudience | None = None,
) -> None:
    """SQL・bind・cause・localsをSDKへ渡さず、固定種別と失敗位置を記録する。"""

    record = realtime_failure_record(error, boundary, audience)
    logfire.error("realtime failure {record}", record=record.model_dump_json(), _exc_info=False)
