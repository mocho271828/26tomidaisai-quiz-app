from collections.abc import Callable

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.contracts import ApiErrorResponse
from quiz_app.domain.errors import (
    AnswerContentConflictError,
    AnswerWindowClosedError,
    ClientReceiptDifferenceError,
    ClientSelectionTimeError,
    DomainInvariantError,
    InvalidStateTransitionError,
    PublicationTooEarlyError,
    ResourceNotFoundError,
    RoundBackupFailedError,
    VersionConflictError,
)
from quiz_app.observability.answer_diagnostics import (
    record_answer_api_error,
    record_answer_failure_stack,
)
from quiz_app.operator.errors import (
    CsrfValidationError,
    DemoAuthDisabledError,
    IdempotencyKeyConflictError,
    InvalidOperatorCredentialsError,
    OperatorAuthorizationError,
    OperatorControlError,
    OperatorSessionInvalidError,
)

ErrorHandler = Callable[[Request, Exception], JSONResponse]


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    record_answer_api_error(status_code, code)
    payload = ApiErrorResponse(code=code, message=message)
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


async def _operator_error_handler(_: Request, error: Exception) -> JSONResponse:
    if isinstance(error, (InvalidOperatorCredentialsError, OperatorSessionInvalidError)):
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            "operator_authentication_required",
            "運営認証を確認してください。",
        )
    if isinstance(error, (CsrfValidationError, OperatorAuthorizationError)):
        return _error_response(
            status.HTTP_403_FORBIDDEN,
            "operator_request_forbidden",
            "この運営操作は許可されていません。",
        )
    if isinstance(error, DemoAuthDisabledError):
        return _error_response(
            status.HTTP_404_NOT_FOUND,
            "demo_auth_not_available",
            "運営認証は利用できません。",
        )
    if isinstance(error, IdempotencyKeyConflictError):
        return _error_response(
            status.HTTP_409_CONFLICT,
            "idempotency_key_conflict",
            "同じ操作キーが異なる要求に使われています。",
        )
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "operator_control_error",
        "運営操作を完了できませんでした。",
    )


async def _domain_error_handler(_: Request, error: Exception) -> JSONResponse:
    record_answer_failure_stack(error, "api_error")
    if isinstance(error, RoundBackupFailedError):
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "round_backup_failed",
            "暗号化backupを検証できませんでした。状態を確認して再試行してください。",
        )
    if isinstance(error, ResourceNotFoundError):
        return _error_response(
            status.HTTP_404_NOT_FOUND,
            "resource_not_found",
            "対象が見つかりません。",
        )
    if isinstance(error, (ClientReceiptDifferenceError, ClientSelectionTimeError)):
        return _error_response(
            status.HTTP_409_CONFLICT,
            "state_conflict",
            "時間の確認ができず、回答を受け付けられませんでした。",
        )
    if isinstance(
        error,
        (
            VersionConflictError,
            InvalidStateTransitionError,
            PublicationTooEarlyError,
            AnswerWindowClosedError,
            AnswerContentConflictError,
        ),
    ):
        return _error_response(
            status.HTTP_409_CONFLICT,
            "state_conflict",
            "表示を更新し、現在の状態を確認してください。",
        )
    if isinstance(error, DomainInvariantError):
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "domain_temporarily_unavailable",
            "現在、操作を完了できません。",
        )
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "domain_error",
        "要求を完了できませんでした。",
    )


async def _database_error_handler(_: Request, error: Exception) -> JSONResponse:
    record_answer_failure_stack(error, "api_error")
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "database_temporarily_unavailable",
        "データベースに接続できません。",
    )


async def _validation_error_handler(_: Request, error: Exception) -> JSONResponse:
    record_answer_failure_stack(error, "api_error")
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "request_validation_failed",
        "入力内容を確認してください。",
    )


def install_operator_error_handlers(application: FastAPI) -> None:
    """運営・domain・DB errorを非機密なHTTP contractへ変換する。"""

    application.add_exception_handler(OperatorControlError, _operator_error_handler)
    from quiz_app.domain.errors import DomainError

    application.add_exception_handler(DomainError, _domain_error_handler)
    application.add_exception_handler(SQLAlchemyError, _database_error_handler)
    application.add_exception_handler(RequestValidationError, _validation_error_handler)
