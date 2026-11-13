"""回答POST/statusだけを、本文や識別子を残さず相関する。"""

import asyncio
import hmac
import json
import re
from contextvars import ContextVar
from datetime import datetime
from functools import lru_cache
from os import getpid
from pathlib import Path
from time import monotonic_ns
from typing import Literal
from uuid import UUID, uuid4

import logfire
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.audience.errors import ParticipantSessionInvalidError
from quiz_app.config import get_settings
from quiz_app.domain.errors import (
    AnswerContentConflictError,
    AnswerWindowClosedError,
    ClientReceiptDifferenceError,
    ClientSelectionTimeError,
    DomainInvariantError,
    InvalidStateTransitionError,
    PublicationTooEarlyError,
    ResourceNotFoundError,
    VersionConflictError,
)

AnswerOperation = Literal["answer_post", "answer_status"]
DiagnosticEvent = Literal[
    "request_started",
    "answer_bound",
    "transaction_started",
    "round_locked",
    "version_checked",
    "db_received",
    "window_validated",
    "answer_replay",
    "commit_started",
    "commit_acknowledged",
    "transaction_failed",
    "integrity_recheck_started",
    "integrity_recheck_finished",
    "status_query_started",
    "status_query_finished",
    "api_error",
    "response_started",
    "request_finished",
    "failure_frame",
]
DiagnosticReason = Literal[
    "version_conflict",
    "answer_window_closed",
    "client_selection_time_invalid",
    "client_receipt_difference_exceeded",
    "answer_content_conflict",
    "resource_not_found",
    "domain_invariant",
    "invalid_transition",
    "publication_too_early",
    "database_error",
    "authentication_required",
    "cancelled",
    "unclassified",
]
ApiCode = Literal[
    "state_conflict",
    "resource_not_found",
    "participant_authentication_required",
    "request_validation_failed",
    "domain_temporarily_unavailable",
    "database_temporarily_unavailable",
    "audience_service_unavailable",
    "audience_control_error",
    "domain_error",
    "other",
]
AnswerStatus = Literal["NotStored", "AcceptedLocked"]
WindowPosition = Literal["unavailable", "before_open", "within_window", "at_or_after_close"]
_API_CODE_ADAPTER = TypeAdapter[ApiCode](ApiCode)
FailureBoundary = Literal["transaction_failed", "request_finished", "api_error"]
_APP_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_FAILURE_FRAMES = 32


def preserve_diagnostic_codes(match: logfire.ScrubMatch) -> str | None:
    """SDK検出で公開codeと既知のsource識別子だけを残し、値とpathを完全一致で限定する。"""

    allowed_values = {
        "reason": {"authentication_required"},
        "api_code": {"participant_authentication_required"},
        "source_module": {
            "quiz_app.audience.auth_service",
            "quiz_app.audience.auth_cache",
        },
        "source_function": {
            "authenticate",
            "authenticate_in_session",
            "get_participant_auth_service",
            "_hash_token",
            "_verify_in_session",
            "get_answer_status_in_session",
            "get_answer_replay_in_session",
        },
    }
    allowed_paths = {
        (*prefix, name): values
        for name, values in allowed_values.items()
        for prefix in (("message",), ("attributes",), ("attributes", "record"))
    }
    values = allowed_paths.get(match.path, set())
    if isinstance(match.value, str) and match.value in values:
        return match.value
    return None


class AnswerDiagnosticRecord(BaseModel):
    """相関・固定語彙・時刻・限定source情報だけを許可し、任意payloadを受け取らない。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    answer_trace: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    clock_domain: str = Field(pattern=r"^[0-9a-f]{32}$")
    monotonic_ns: int = Field(ge=0)
    operation: AnswerOperation
    event: DiagnosticEvent
    reason: DiagnosticReason | None = None
    api_code: ApiCode | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    status: AnswerStatus | None = None
    db_received_at: datetime | None = None
    window_position: WindowPosition | None = None
    is_replay: bool | None = None
    transaction_phase: Literal["body", "commit"] | None = None
    failure_boundary: FailureBoundary | None = None
    source_module: str | None = Field(
        default=None, max_length=100, pattern=r"^quiz_app(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    source_function: str | None = Field(
        default=None, max_length=100, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    source_line: int | None = Field(default=None, gt=0)
    frame_index: int | None = Field(default=None, ge=0, lt=_MAX_FAILURE_FRAMES)


_CONSOLE_RECORD_TEMPLATE = (
    "answer diagnostic {{"
    + ",".join(f'"{name}":{{{name}}}' for name in AnswerDiagnosticRecord.model_fields)
    + "}}"
)


@lru_cache(maxsize=1)
def _clock_domain(process_id: int) -> str:
    # preforkでも別processを同一clockとして比較しない。PID自体は記録しない。
    del process_id
    return uuid4().hex


def diagnostic_reason(error: BaseException | None) -> DiagnosticReason:
    """例外本文・class名を記録せず、既知の失敗種類だけを返す。"""

    reasons: tuple[tuple[type[BaseException], DiagnosticReason], ...] = (
        (VersionConflictError, "version_conflict"),
        (ClientSelectionTimeError, "client_selection_time_invalid"),
        (ClientReceiptDifferenceError, "client_receipt_difference_exceeded"),
        (AnswerWindowClosedError, "answer_window_closed"),
        (AnswerContentConflictError, "answer_content_conflict"),
        (ResourceNotFoundError, "resource_not_found"),
        (DomainInvariantError, "domain_invariant"),
        (InvalidStateTransitionError, "invalid_transition"),
        (PublicationTooEarlyError, "publication_too_early"),
        (SQLAlchemyError, "database_error"),
        (ParticipantSessionInvalidError, "authentication_required"),
        (asyncio.CancelledError, "cancelled"),
    )
    return next((reason for kind, reason in reasons if isinstance(error, kind)), "unclassified")


class AnswerDiagnostic:
    """requestだけを保持し、回答ごとの無期限cacheやmetric labelを作らない。"""

    def __init__(self, operation: AnswerOperation) -> None:
        self.request_id = uuid4().hex
        self.operation = operation
        self.answer_trace: str | None = None
        self.clock_domain = _clock_domain(getpid())

    def bind(self, session_id: UUID, client_answer_id: UUID) -> None:
        # 認証用secretの用途を分離し、session/回答UUIDの生値や単純hashを残さない。
        key = hmac.digest(
            get_settings().session_secret.get_secret_value().encode(),
            b"quiz.answer-diagnostics.v1",
            "sha256",
        )
        self.answer_trace = hmac.digest(
            key, session_id.bytes + client_answer_id.bytes, "sha256"
        ).hex()
        self.emit("answer_bound")

    def response_headers(self) -> list[tuple[bytes, bytes]]:
        headers = [(b"x-quiz-request-id", self.request_id.encode("ascii"))]
        if self.answer_trace is not None:
            headers.append((b"x-quiz-answer-trace", self.answer_trace.encode("ascii")))
        return headers

    def emit(
        self,
        event: DiagnosticEvent,
        *,
        reason: DiagnosticReason | None = None,
        api_code: ApiCode | None = None,
        http_status: int | None = None,
        status: AnswerStatus | None = None,
        db_received_at: datetime | None = None,
        window_position: WindowPosition | None = None,
        is_replay: bool | None = None,
        transaction_phase: Literal["body", "commit"] | None = None,
        failure_boundary: FailureBoundary | None = None,
        source_module: str | None = None,
        source_function: str | None = None,
        source_line: int | None = None,
        frame_index: int | None = None,
    ) -> None:
        try:
            record = AnswerDiagnosticRecord(
                request_id=self.request_id,
                answer_trace=self.answer_trace,
                clock_domain=self.clock_domain,
                monotonic_ns=monotonic_ns(),
                operation=self.operation,
                event=event,
                reason=reason,
                api_code=api_code,
                http_status=http_status,
                status=status,
                db_received_at=db_received_at,
                window_position=window_position,
                is_replay=is_replay,
                transaction_phase=transaction_phase,
                failure_boundary=failure_boundary,
                source_module=source_module,
                source_function=source_function,
                source_line=source_line,
                frame_index=frame_index,
            )
        except ValidationError:
            # 計測の型不一致で回答transactionを失敗させず、入力値を出さず欠測を示す。
            logfire.warning("answer diagnostic record invalid")
            return
        # Logfireは長い単一引数をconsoleで省略するため、短いJSON値ごとに展開する。
        # 構造化exportには完全なrecordを残し、consoleでも同じschemaで再検証できる。
        logfire.info(
            _CONSOLE_RECORD_TEMPLATE,
            record=record.model_dump_json(exclude_none=True),
            **{name: json.dumps(value) for name, value in record.model_dump(mode="json").items()},
        )


current_answer_diagnostic: ContextVar[AnswerDiagnostic | None] = ContextVar(
    "answer_diagnostic", default=None
)


def record_answer_failure_stack(error: BaseException, boundary: FailureBoundary) -> None:
    """アプリ内の失敗経路だけを最大32frameへ絞り、本文・locals・causeを読まない。"""

    diagnostic = current_answer_diagnostic.get()
    if diagnostic is None:
        return
    traceback = error.__traceback__
    frame_index = 0
    while traceback is not None and frame_index < _MAX_FAILURE_FRAMES:
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
        diagnostic.emit(
            "failure_frame",
            reason=diagnostic_reason(error),
            failure_boundary=boundary,
            source_module=".".join(("quiz_app", *parts)),
            source_function=code.co_name,
            source_line=line,
            frame_index=frame_index,
        )
        frame_index += 1


def bind_answer_diagnostic(session_id: UUID, client_answer_id: UUID) -> None:
    diagnostic = current_answer_diagnostic.get()
    if diagnostic is not None:
        diagnostic.bind(session_id, client_answer_id)


def record_database_receipt(
    received_at: datetime,
    open_at: datetime | None,
    close_at: datetime | None,
) -> None:
    """既存DB時刻の位置だけを記録し、受付可否の正本はdomain検証に残す。"""

    if current_answer_diagnostic.get() is None:
        return
    position: WindowPosition
    if open_at is None or close_at is None:
        position = "unavailable"
    elif received_at < open_at:
        position = "before_open"
    elif received_at >= close_at:
        position = "at_or_after_close"
    else:
        position = "within_window"
    record_answer_event("db_received", db_received_at=received_at, window_position=position)


def record_answer_event(
    event: DiagnosticEvent,
    *,
    reason: DiagnosticReason | None = None,
    status: AnswerStatus | None = None,
    db_received_at: datetime | None = None,
    window_position: WindowPosition | None = None,
    is_replay: bool | None = None,
    transaction_phase: Literal["body", "commit"] | None = None,
) -> None:
    diagnostic = current_answer_diagnostic.get()
    if diagnostic is not None:
        diagnostic.emit(
            event,
            reason=reason,
            status=status,
            db_received_at=db_received_at,
            window_position=window_position,
            is_replay=is_replay,
            transaction_phase=transaction_phase,
        )


def record_answer_api_error(status_code: int, code: str) -> None:
    """公開応答codeも固定集合に限定し、未知のcodeを任意文字列として残さない。"""

    diagnostic = current_answer_diagnostic.get()
    if diagnostic is not None:
        try:
            safe_code = _API_CODE_ADAPTER.validate_python(code)
        except ValidationError:
            safe_code = "other"
        diagnostic.emit("api_error", api_code=safe_code, http_status=status_code)
