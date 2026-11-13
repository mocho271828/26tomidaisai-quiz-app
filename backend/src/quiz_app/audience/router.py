from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Response, Security, status
from fastapi.security import APIKeyCookie

from quiz_app.audience.async_service import AudienceAsyncService, get_audience_async_service
from quiz_app.audience.auth_service import (
    ParticipantAuthService,
    get_participant_auth_service,
)
from quiz_app.audience.contracts import (
    AnswerReceiptResponse,
    AnswerStatusResponse,
    ParticipantJoinRequest,
    ParticipantStateResponse,
    PersonalResultResponse,
    SubmitAnswerRequest,
    TimeResponse,
)
from quiz_app.audience.projection import (
    AudienceProjectionService,
    get_audience_projection_service,
)
from quiz_app.config import PARTICIPANT_SESSION_COOKIE_NAME, Settings, get_settings
from quiz_app.contracts import ApiErrorResponse
from quiz_app.observability.request_timing import (
    mark_handler_dispatch,
    observe_handler,
)
from quiz_app.realtime.answer_notifications import (
    AnswerNotificationCoalescer,
    get_answer_notifications,
)
from quiz_app.realtime.redis_adapter import (
    RealtimePublisher,
    audiences_for_invalidation,
    get_realtime_publisher,
)
from quiz_app.single_event import require_single_event_id

participant_session_cookie = APIKeyCookie(
    name=PARTICIPANT_SESSION_COOKIE_NAME,
    scheme_name="ParticipantSessionCookie",
    auto_error=False,
)
router = APIRouter(tags=["audience"])
AUDIENCE_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {"model": ApiErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ApiErrorResponse},
}
ANSWER_DIAGNOSTIC_RESPONSES = {
    response_status: {
        **contract,
        "headers": {
            "X-Quiz-Request-Id": {
                "description": "サーバー生成の要求相関値。認証情報として使用しない。",
                "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            },
            "X-Quiz-Answer-Trace": {
                "description": "認証後だけ付与する回答相関HMAC。生の識別子を含まない。",
                "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
    }
    for response_status, contract in {
        status.HTTP_200_OK: {},
        **AUDIENCE_ERROR_RESPONSES,
    }.items()
}
NO_STORE = "no-store"


def prevent_response_storage(response: Response) -> None:
    response.headers["Cache-Control"] = NO_STORE


@router.post(
    "/api/v1/participant/join",
    response_model=ParticipantStateResponse,
    responses=AUDIENCE_ERROR_RESPONSES,
)
def join_event(
    request: ParticipantJoinRequest,
    response: Response,
    auth_service: Annotated[ParticipantAuthService, Depends(get_participant_auth_service)],
    projection_service: Annotated[
        AudienceProjectionService,
        Depends(get_audience_projection_service),
    ],
    settings: Annotated[Settings, Depends(get_settings)],
    realtime_publisher: Annotated[RealtimePublisher, Depends(get_realtime_publisher)],
    event_id: Annotated[UUID, Depends(require_single_event_id)],
    existing_session_token: Annotated[
        str | None,
        Cookie(alias=PARTICIPANT_SESSION_COOKIE_NAME),
    ] = None,
) -> ParticipantStateResponse:
    prevent_response_storage(response)
    created = auth_service.join(
        event_id,
        request.client_join_token,
        existing_session_token,
    )
    response.set_cookie(
        key=PARTICIPANT_SESSION_COOKIE_NAME,
        value=created.session_token,
        expires=created.expires_at.astimezone(UTC),
        path="/api/v1/participant",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    realtime_publisher.notify(
        created.principal.event_id,
        audiences_for_invalidation("participant_join"),
        cause="participant_join",
    )
    return projection_service.get_participant_state(created.principal)


@router.get(
    "/api/v1/participant/session",
    response_model=ParticipantStateResponse,
    responses=AUDIENCE_ERROR_RESPONSES,
)
async def get_participant_session(
    response: Response,
    session_token: Annotated[str | None, Security(participant_session_cookie)],
    audience_service: Annotated[
        AudienceAsyncService,
        Depends(get_audience_async_service),
    ],
    _timing_dispatch: Annotated[None, Depends(mark_handler_dispatch)],
) -> ParticipantStateResponse:
    with observe_handler(is_async=True):
        prevent_response_storage(response)
        return await audience_service.get_participant_state(session_token)


@router.get(
    "/api/v1/participant/result",
    response_model=PersonalResultResponse,
    responses=AUDIENCE_ERROR_RESPONSES,
)
async def get_participant_result(
    response: Response,
    session_token: Annotated[str | None, Security(participant_session_cookie)],
    audience_service: Annotated[AudienceAsyncService, Depends(get_audience_async_service)],
    _timing_dispatch: Annotated[None, Depends(mark_handler_dispatch)],
) -> PersonalResultResponse:
    with observe_handler(is_async=True):
        prevent_response_storage(response)
        return await audience_service.get_participant_result(session_token)


@router.post(
    "/api/v1/participant/rounds/{round_id}/answers",
    response_model=AnswerReceiptResponse,
    responses=ANSWER_DIAGNOSTIC_RESPONSES,
)
async def submit_answer(
    round_id: UUID,
    request: SubmitAnswerRequest,
    response: Response,
    session_token: Annotated[str | None, Security(participant_session_cookie)],
    audience_service: Annotated[
        AudienceAsyncService,
        Depends(get_audience_async_service),
    ],
    answer_notifications: Annotated[AnswerNotificationCoalescer, Depends(get_answer_notifications)],
    _timing_dispatch: Annotated[None, Depends(mark_handler_dispatch)],
) -> AnswerReceiptResponse:
    with observe_handler(is_async=True):
        prevent_response_storage(response)
        committed = await audience_service.submit_answer(session_token, round_id, request)
        accepted = committed.answer
        response_contract = AnswerReceiptResponse(
            answer_id=accepted.answer_id,
            round_id=accepted.round_id,
            option_id=accepted.option_id,
            client_answer_id=accepted.client_answer_id,
            received_at=accepted.received_at,
            client_elapsed_ms=accepted.client_elapsed_ms,
            timing_basis=accepted.timing_basis,
            is_replay=accepted.is_replay,
        )
        # commit済み回答の通知を預け、Redis I/Oを受付応答へ含めない。
        answer_notifications.enqueue(committed.event_id)
        return response_contract


@router.get(
    "/api/v1/participant/answers/{client_answer_id}/status",
    response_model=AnswerStatusResponse,
    responses=ANSWER_DIAGNOSTIC_RESPONSES,
)
async def get_answer_status(
    client_answer_id: UUID,
    response: Response,
    session_token: Annotated[str | None, Security(participant_session_cookie)],
    audience_service: Annotated[
        AudienceAsyncService,
        Depends(get_audience_async_service),
    ],
    _timing_dispatch: Annotated[None, Depends(mark_handler_dispatch)],
) -> AnswerStatusResponse:
    with observe_handler(is_async=True):
        prevent_response_storage(response)
        return await audience_service.get_answer_status(session_token, client_answer_id)


@router.get(
    "/api/v1/time",
    response_model=TimeResponse,
    responses=AUDIENCE_ERROR_RESPONSES,
)
async def get_server_time(
    response: Response,
    audience_service: Annotated[
        AudienceAsyncService,
        Depends(get_audience_async_service),
    ],
    _timing_dispatch: Annotated[None, Depends(mark_handler_dispatch)],
) -> TimeResponse:
    with observe_handler(is_async=True):
        prevent_response_storage(response)
        return await audience_service.get_server_time()
