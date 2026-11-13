import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

from anyio import CancelScope
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from quiz_app.audience.async_service import AudienceAsyncService, get_audience_async_service
from quiz_app.audience.auth_service import VerifiedParticipantAuthentication
from quiz_app.audience.contracts import (
    ParticipantCommonStateResponse,
    PersonalResultDelivery,
    PersonalResultRevision,
)
from quiz_app.audience.errors import AudienceServiceUnavailableError, ParticipantSessionInvalidError
from quiz_app.audience.projection import get_audience_projection_service
from quiz_app.audience.result_delivery import PersonalResultService
from quiz_app.config import (
    OPERATOR_SESSION_COOKIE_NAME,
    PARTICIPANT_SESSION_COOKIE_NAME,
    Settings,
    get_settings,
)
from quiz_app.observability import QuizMetrics
from quiz_app.observability.realtime_failures import RealtimeFailureBoundary, log_realtime_failure
from quiz_app.operator.auth_service import (
    OperatorAuthService,
    VerifiedOperatorAuthentication,
    get_operator_auth_service,
)
from quiz_app.operator.errors import OperatorControlError
from quiz_app.realtime.contracts import (
    HeartbeatMessage,
    OperatorStateMessage,
    ParticipantResultMessage,
    ParticipantStateMessage,
    PollingRequiredMessage,
    RealtimeAudience,
    SessionChangedMessage,
)
from quiz_app.realtime.redis_adapter import RealtimeUnavailableError
from quiz_app.realtime.state_stream import (
    SharedState,
    StateSnapshotRefreshRequired,
    StateStreamFactory,
    StateSubscription,
    get_state_stream_factory,
)
from quiz_app.single_event import (
    SingleEventConfigurationError,
    SingleEventService,
    get_single_event_service,
)

router = APIRouter(tags=["realtime"])
ORIGIN_REJECTED_CLOSE_CODE = 1008
UNSUPPORTED_CLIENT_MESSAGE_CLOSE_CODE = 1003
AUTH_REQUIRED_CLOSE_CODE = 4401
REALTIME_UNAVAILABLE_CLOSE_CODE = 1013
HEARTBEAT_SECONDS = 5.0
SEND_TIMEOUT_SECONDS = 1.0
PARTICIPANT_REVALIDATION_SECONDS = 60.0
OPERATOR_REVALIDATION_SECONDS = 1.0
VerifiedAuthentication = VerifiedParticipantAuthentication | VerifiedOperatorAuthentication
StateWaitTask = (
    asyncio.Task[dict[str, object]]
    | asyncio.Task[SharedState]
    | asyncio.Task[StateSubscription]
    | asyncio.Task[int]
)


class ReauthenticationRequired(Exception):
    """送信開始前の認証期限到来は、transport障害と分けて再確認する。"""


def is_allowed_origin(origin: str | None, settings: Settings) -> bool:
    """Originをwildcardなしの設定値と完全一致で検証する。"""

    if origin is None:
        return False
    return origin in {str(allowed).rstrip("/") for allowed in settings.realtime_allowed_origins}


async def _reject_disallowed_origin(websocket: WebSocket, settings: Settings) -> bool:
    if is_allowed_origin(websocket.headers.get("origin"), settings):
        return False
    await websocket.close(code=ORIGIN_REJECTED_CLOSE_CODE)
    return True


def _metrics_for_websocket(websocket: WebSocket) -> QuizMetrics | None:
    application = getattr(websocket, "scope", {}).get("app")
    state = getattr(application, "state", None)
    metrics = getattr(state, "quiz_metrics", None)
    return metrics if isinstance(metrics, QuizMetrics) else None


async def _send_polling_fallback(websocket: WebSocket, audience: RealtimeAudience) -> None:
    metrics = _metrics_for_websocket(websocket)
    if metrics is not None:
        metrics.record_polling_fallback(audience=audience)
    fallback = PollingRequiredMessage(
        type="polling.required", reason="realtime_unavailable", poll_after_ms=5_000
    )
    try:
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            await websocket.send_json(fallback.model_dump(mode="json"))
    except (RuntimeError, WebSocketDisconnect, TimeoutError):
        pass
    await _close_bounded(websocket, REALTIME_UNAVAILABLE_CLOSE_CODE)


async def _close_bounded(websocket: WebSocket, code: int) -> None:
    try:
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect, TimeoutError):
        return


def authentication_deadlines(
    verified: VerifiedAuthentication, revalidation_seconds: float
) -> tuple[float, float]:
    remaining = (verified.expires_at - verified.database_time).total_seconds()
    absolute_until = verified.verified_from + remaining
    return verified.verified_from + min(revalidation_seconds, remaining), absolute_until


async def _serve_states(
    websocket: WebSocket,
    audience: RealtimeAudience,
    event_id: UUID,
    subscription_factory: StateStreamFactory,
    verified: VerifiedAuthentication,
    verify: Callable[[], VerifiedAuthentication] | None = None,
    *,
    async_verify: Callable[[], Awaitable[VerifiedAuthentication]] | None = None,
    clock: Callable[[], float] = monotonic,
    personal_results: PersonalResultService | None = None,
) -> None:
    if verify is None and async_verify is None:
        raise ValueError("realtime_verifier_required")
    await websocket.accept()
    subscription: StateSubscription | None = None
    subscribe_task: asyncio.Task[StateSubscription] | None = None
    receive_task: asyncio.Task[dict[str, object]] | None = None
    message_task: asyncio.Task[SharedState] | None = None
    personal_task: asyncio.Task[int] | None = None
    personal_generation = personal_results.generation if personal_results is not None else 0
    personal_pending = False
    sent_personal_revision: PersonalResultRevision | None = None
    metrics = _metrics_for_websocket(websocket)
    stream_id = uuid4()
    sequence = 0
    state_pending = False
    session_change_pending = False
    timeout_boundary: RealtimeFailureBoundary = "initial_snapshot"
    interval = (
        PARTICIPANT_REVALIDATION_SECONDS
        if audience == "participant"
        else OPERATOR_REVALIDATION_SECONDS
    )
    revalidate_at, absolute_until = authentication_deadlines(verified, interval)
    heartbeat_at = clock() + HEARTBEAT_SECONDS

    async def send(
        message: ParticipantStateMessage
        | OperatorStateMessage
        | HeartbeatMessage
        | SessionChangedMessage
        | ParticipantResultMessage,
        boundary: RealtimeFailureBoundary,
        *,
        state_until: float | None = None,
        still_current: Callable[[], bool] | None = None,
    ) -> None:
        nonlocal timeout_boundary
        timeout_boundary = boundary
        payload = message.model_dump_json()
        # JSON文字列化も期限を跨ぐため、変換をすべて終えてから認証と鮮度を再確認する。
        now = clock()
        if now >= absolute_until:
            raise TimeoutError("realtime_absolute_deadline_reached")
        if now >= revalidate_at:
            raise ReauthenticationRequired
        remaining = min(SEND_TIMEOUT_SECONDS, revalidate_at - now, absolute_until - now)
        if state_until is not None:
            if now >= state_until:
                raise StateSnapshotRefreshRequired("state_snapshot_expired")
            remaining = min(remaining, state_until - now)
        if still_current is not None and not still_current():
            raise StateSnapshotRefreshRequired("personal_result_superseded")
        started = clock()
        kind = (
            "personal_result"
            if isinstance(message, ParticipantResultMessage)
            else (
                "heartbeat"
                if isinstance(message, HeartbeatMessage)
                else ("control" if isinstance(message, SessionChangedMessage) else "state")
            )
        )
        try:
            async with asyncio.timeout(remaining):
                await websocket.send_text(payload)
        except TimeoutError:
            if metrics is not None:
                metrics.record_realtime_delivery(
                    audience=audience,
                    kind=kind,
                    outcome="timeout",
                    duration_seconds=clock() - started,
                )
            raise
        else:
            if metrics is not None:
                metrics.record_realtime_delivery(
                    audience=audience, kind=kind, outcome="sent", duration_seconds=clock() - started
                )

    try:
        if clock() >= absolute_until:
            await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
            return
        receive_task = asyncio.create_task(websocket.receive())
        subscribe_task = asyncio.create_task(subscription_factory.subscribe(audience, event_id))
        while True:
            if subscription is not None and message_task is None and not state_pending:
                message_task = asyncio.create_task(subscription.next_message())
            if personal_results is not None and personal_task is None:
                personal_task = asyncio.create_task(
                    personal_results.wait_for_change(personal_generation)
                )
            waiting: set[StateWaitTask] = {receive_task}
            if personal_task is not None:
                waiting.add(personal_task)
            if subscribe_task is not None:
                waiting.add(subscribe_task)
            if message_task is not None:
                waiting.add(message_task)
            done, _ = await asyncio.wait(
                waiting,
                timeout=max(0.0, min(heartbeat_at, revalidate_at, absolute_until) - clock()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                if receive_task.result()["type"] != "websocket.disconnect":
                    await _close_bounded(websocket, UNSUPPORTED_CLIENT_MESSAGE_CLOSE_CODE)
                return
            if clock() >= absolute_until:
                await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
                return
            if clock() >= revalidate_at:
                previous_principal = verified.principal
                timeout_boundary = "authentication_refresh"
                async with asyncio.timeout(min(SEND_TIMEOUT_SECONDS, absolute_until - clock())):
                    if async_verify is not None:
                        verified = await async_verify()
                    else:
                        assert verify is not None
                        verified = await run_in_threadpool(verify)
                if clock() >= absolute_until:
                    await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
                    return
                revalidate_at, absolute_until = authentication_deadlines(verified, interval)
                if clock() >= absolute_until:
                    await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
                    return
                if clock() >= revalidate_at:
                    raise RealtimeUnavailableError("realtime_authentication_too_slow")
                if (
                    isinstance(verified, VerifiedParticipantAuthentication)
                    and verified.principal.event_id != event_id
                ):
                    await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
                    return
                if previous_principal != verified.principal:
                    session_change_pending = True
                    sent_personal_revision = None
                    personal_pending = True
            if personal_task is not None and personal_task in done:
                personal_generation = personal_task.result()
                personal_task = None
                personal_pending = True
            if subscribe_task in done:
                subscription = subscribe_task.result()
                subscribe_task = None
            if message_task is not None and message_task in done and subscription is not None:
                message_task.result()
                message_task = None
                state_pending = True
                personal_pending = True
            try:
                if session_change_pending:
                    await send(
                        SessionChangedMessage(
                            type="session.changed",
                            audience=audience,
                            event_id=event_id,
                            stream_id=stream_id,
                            sequence=sequence + 1,
                        ),
                        "session_change_send",
                    )
                    sequence += 1
                    session_change_pending = False
                if clock() >= heartbeat_at:
                    # 鮮度不明を省略すると、端末に残るready版がHTTP補完を起動してしまう。
                    delivery = (
                        PersonalResultDelivery(status="unavailable", revision=None)
                        if audience == "participant"
                        else None
                    )
                    if subscription is not None and personal_results is not None:
                        try:
                            common, _ = subscription.snapshot_for_send()
                        except StateSnapshotRefreshRequired:
                            pass
                        else:
                            if isinstance(common, ParticipantCommonStateResponse):
                                delivery = personal_results.delivery(common)
                    await send(
                        HeartbeatMessage(
                            type="heartbeat",
                            audience=audience,
                            event_id=event_id,
                            stream_id=stream_id,
                            sequence=sequence + 1,
                            personal_result_delivery=delivery,
                        ),
                        "heartbeat_send",
                    )
                    sequence += 1
                    heartbeat_at = clock() + HEARTBEAT_SECONDS
                    personal_pending = True
                if state_pending and subscription is not None:
                    try:
                        state, state_until = subscription.snapshot_for_send()
                        if audience == "participant" and isinstance(
                            state, ParticipantCommonStateResponse
                        ):
                            if personal_results is not None:
                                state = state.model_copy(
                                    update={
                                        "personal_result_delivery": personal_results.delivery(state)
                                    }
                                )
                            message = ParticipantStateMessage(
                                type="participant.state",
                                audience="participant",
                                event_id=event_id,
                                stream_id=stream_id,
                                sequence=sequence + 1,
                                state=state,
                            )
                        elif audience == "operator" and not isinstance(
                            state, ParticipantCommonStateResponse
                        ):
                            message = OperatorStateMessage(
                                type="operator.state",
                                audience="operator",
                                event_id=event_id,
                                stream_id=stream_id,
                                sequence=sequence + 1,
                                state=state,
                            )
                        else:
                            raise RealtimeUnavailableError("realtime_snapshot_audience_mismatch")
                        await send(message, "state_send", state_until=state_until)
                        sequence += 1
                        state_pending = False
                    except StateSnapshotRefreshRequired:
                        # 共通readの待機中もheartbeatとsession再確認を進める。
                        state_pending = False
                        message_task = asyncio.create_task(subscription.fresh_message())
                if (
                    personal_pending
                    and subscription is not None
                    and personal_results is not None
                    and isinstance(verified, VerifiedParticipantAuthentication)
                ):
                    personal_pending = False
                    try:
                        common, state_until = subscription.snapshot_for_send()
                        if isinstance(common, ParticipantCommonStateResponse):
                            delivery = personal_results.delivery(common)
                            # 既送信の同版だけ省き、未送信・構築中の本人検証は従来どおり行う。
                            if (
                                delivery.status == "ready"
                                and delivery.revision == sent_personal_revision
                            ):
                                continue
                            result = personal_results.result_for(verified.principal, common)
                            revision = result.delivery.revision
                            if (
                                result.delivery.status == "ready"
                                and revision != sent_personal_revision
                            ):
                                await send(
                                    ParticipantResultMessage(
                                        type="participant.result",
                                        audience="participant",
                                        event_id=event_id,
                                        stream_id=stream_id,
                                        sequence=sequence + 1,
                                        result=result,
                                    ),
                                    "state_send",
                                    state_until=state_until,
                                    still_current=lambda common=common, result=result: (
                                        personal_results.delivery(common) == result.delivery
                                    ),
                                )
                                sequence += 1
                                sent_personal_revision = revision
                    except StateSnapshotRefreshRequired:
                        # 非公開化/新しい問題の確認を先行し、旧本人payloadは保持しない。
                        if metrics is not None:
                            metrics.record_realtime_delivery(
                                audience=audience, kind="personal_result", outcome="discarded"
                            )
            except ReauthenticationRequired:
                personal_pending = True
                # まだtransportへ渡していない状態/controlとheartbeat期限を保持する。
                continue
    except (ParticipantSessionInvalidError, OperatorControlError):
        await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
    except TimeoutError as error:
        log_realtime_failure(error, timeout_boundary, audience=audience)
        if clock() >= absolute_until:
            await _close_bounded(websocket, AUTH_REQUIRED_CLOSE_CODE)
        else:
            await _send_polling_fallback(websocket, audience)
    except (
        RealtimeUnavailableError,
        SQLAlchemyError,
        AudienceServiceUnavailableError,
        SingleEventConfigurationError,
    ) as error:
        log_realtime_failure(
            error,
            "initial_snapshot" if subscription is None else "state_or_authentication",
            audience=audience,
        )
        await _send_polling_fallback(websocket, audience)
    except WebSocketDisconnect:
        return
    finally:
        for task in (receive_task, message_task, subscribe_task, personal_task):
            if task is not None and not task.done():
                task.cancel()
        # ASGIのcancel scopeがcleanupのawaitを再度cancelして、FD回収を飛ばすのを防ぐ。
        with CancelScope(shield=True):
            await asyncio.gather(
                *(
                    task
                    for task in (receive_task, message_task, subscribe_task, personal_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            if (
                subscription is None
                and subscribe_task is not None
                and not subscribe_task.cancelled()
            ):
                # disconnectと購読完了が同時だった場合も、取得済みFDを回収する。
                result = subscribe_task.exception()
                if result is None:
                    subscription = subscribe_task.result()
            if subscription is not None:
                await subscription.close()


@router.websocket("/api/v1/participant/realtime")
async def participant_realtime(
    websocket: WebSocket,
    audience_service: Annotated[AudienceAsyncService, Depends(get_audience_async_service)],
    subscription_factory: Annotated[StateStreamFactory, Depends(get_state_stream_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if await _reject_disallowed_origin(websocket, settings):
        return
    session_token = websocket.cookies.get(PARTICIPANT_SESSION_COOKIE_NAME)
    if session_token is None:
        await websocket.close(code=AUTH_REQUIRED_CLOSE_CODE)
        return
    try:
        verified = await audience_service.authenticate_realtime(session_token)
    except ParticipantSessionInvalidError:
        await websocket.close(code=AUTH_REQUIRED_CLOSE_CODE)
        return
    except (
        SingleEventConfigurationError,
        SQLAlchemyError,
        AudienceServiceUnavailableError,
    ) as error:
        log_realtime_failure(error, "single_event", audience="participant")
        await websocket.close(code=REALTIME_UNAVAILABLE_CLOSE_CODE)
        return
    await _serve_states(
        websocket,
        "participant",
        verified.principal.event_id,
        subscription_factory,
        verified,
        async_verify=lambda: audience_service.authenticate_realtime(session_token),
        personal_results=get_audience_projection_service().personal_results,
    )


@router.websocket("/api/v1/operator/realtime")
async def operator_realtime(
    websocket: WebSocket,
    auth_service: Annotated[OperatorAuthService, Depends(get_operator_auth_service)],
    subscription_factory: Annotated[StateStreamFactory, Depends(get_state_stream_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    single_event_service: Annotated[SingleEventService, Depends(get_single_event_service)],
) -> None:
    if await _reject_disallowed_origin(websocket, settings):
        return
    session_token = websocket.cookies.get(OPERATOR_SESSION_COOKIE_NAME)
    if session_token is None:
        await websocket.close(code=AUTH_REQUIRED_CLOSE_CODE)
        return
    try:
        verified = await run_in_threadpool(auth_service.authenticate_realtime, session_token)
    except OperatorControlError:
        await websocket.close(code=AUTH_REQUIRED_CLOSE_CODE)
        return
    try:
        event_id = await run_in_threadpool(single_event_service.get_event_id)
    except (SingleEventConfigurationError, SQLAlchemyError) as error:
        log_realtime_failure(error, "single_event", audience="operator")
        await websocket.close(code=REALTIME_UNAVAILABLE_CLOSE_CODE)
        return
    await _serve_states(
        websocket,
        "operator",
        event_id,
        subscription_factory,
        verified,
        lambda: auth_service.authenticate_realtime(session_token),
    )
