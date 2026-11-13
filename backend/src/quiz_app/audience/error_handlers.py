from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from quiz_app.audience.errors import (
    AudienceControlError,
    AudienceServiceUnavailableError,
    EventJoinUnavailableError,
    ParticipantIdExhaustedError,
    ParticipantSessionInvalidError,
)
from quiz_app.contracts import ApiErrorResponse
from quiz_app.observability.answer_diagnostics import (
    record_answer_api_error,
    record_answer_failure_stack,
)


def _response(status_code: int, code: str, message: str) -> JSONResponse:
    record_answer_api_error(status_code, code)
    payload = ApiErrorResponse(code=code, message=message)
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers={
            "Cache-Control": "no-store",
            **({"Retry-After": "5"} if status_code == status.HTTP_503_SERVICE_UNAVAILABLE else {}),
        },
    )


async def _audience_error_handler(_: Request, error: Exception) -> JSONResponse:
    record_answer_failure_stack(error, "api_error")
    if isinstance(error, AudienceServiceUnavailableError):
        return _response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "audience_service_unavailable",
            "一時的に要求を完了できません。時間をおいて再試行してください。",
        )
    if isinstance(error, ParticipantSessionInvalidError):
        return _response(
            status.HTTP_401_UNAUTHORIZED,
            "participant_authentication_required",
            "参加者セッションを確認してください。",
        )
    if isinstance(error, EventJoinUnavailableError):
        return _response(
            status.HTTP_409_CONFLICT,
            "event_join_unavailable",
            "現在、このイベントには参加できません。",
        )
    if isinstance(error, ParticipantIdExhaustedError):
        return _response(
            status.HTTP_409_CONFLICT,
            "participant_id_exhausted",
            "このイベントで割り当て可能な参加者IDをすべて使用しました。運営にお問い合わせください。",
        )
    return _response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "audience_control_error",
        "要求を完了できませんでした。",
    )


def install_audience_error_handlers(application: FastAPI) -> None:
    application.add_exception_handler(AudienceControlError, _audience_error_handler)
