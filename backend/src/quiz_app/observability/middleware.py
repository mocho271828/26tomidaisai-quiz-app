"""Pure ASGI instrumentation with a deliberately bounded label vocabulary."""

from __future__ import annotations

import asyncio
import re
import sys
from time import monotonic
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from quiz_app.observability.answer_diagnostics import (
    AnswerDiagnostic,
    current_answer_diagnostic,
    diagnostic_reason,
    record_answer_failure_stack,
)
from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.request_timing import RequestTiming, current_request_timing

METRICS_PATH: Final = "/internal/metrics"
_SAFE_ROUTE_TEMPLATE = re.compile(r"/[A-Za-z0-9_{}./-]{0,159}\Z")
_KNOWN_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_PARTICIPANT_WEBSOCKET_PATH: Final = "/api/v1/participant/realtime"
_OPERATOR_WEBSOCKET_PATH: Final = "/api/v1/operator/realtime"
_CLOSE_OUTCOMES: Final = {
    1000: "normal",
    1003: "unsupported_data",
    1008: "origin_rejected",
    1013: "realtime_unavailable",
    4401: "authentication_required",
}
_TIMED_PARTICIPANT_PATH = re.compile(
    r"/api/v1/(?:time|participant/(?:session|rounds/[^/]+/answers|answers/[^/]+/status))\Z"
)


def _method(scope: Scope) -> str:
    candidate = scope.get("method")
    if isinstance(candidate, str) and candidate in _KNOWN_METHODS:
        return candidate
    return "OTHER"


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    candidate = getattr(route, "path", None)
    if isinstance(candidate, str) and _SAFE_ROUTE_TEMPLATE.fullmatch(candidate):
        return candidate
    return "unmatched"


def _status_class(status: int | None) -> str:
    if status is None or status < 100 or status > 599:
        return "other"
    return f"{status // 100}xx"


def _websocket_audience(scope: Scope) -> str:
    path = scope.get("path")
    if not isinstance(path, str):
        return "unmatched"
    if path == _PARTICIPANT_WEBSOCKET_PATH:
        return "participant"
    if path == _OPERATOR_WEBSOCKET_PATH:
        return "operator"
    return "unmatched"


def _websocket_code_class(code: int | None) -> str:
    if code is None or code < 1000 or code > 4999:
        return "other"
    return f"{code // 1000}xxx"


class PrometheusMiddleware:
    """Record HTTP and WebSocket signals without buffering ASGI messages."""

    def __init__(self, app: ASGIApp, metrics: QuizMetrics) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "http":
            if scope.get("path") == METRICS_PATH:
                await self._app(scope, receive, send)
                return
            await self._observe_http(scope, receive, send)
            return
        if scope_type == "websocket":
            await self._observe_websocket(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _observe_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = _method(scope)
        status: int | None = None
        started_at = monotonic()
        path = scope.get("path")
        timing = (
            RequestTiming(started_at=started_at)
            if isinstance(path, str) and _TIMED_PARTICIPANT_PATH.fullmatch(path)
            else None
        )
        timing_token = current_request_timing.set(timing)
        diagnostic = None
        if isinstance(path, str):
            if method == "POST" and re.fullmatch(r"/api/v1/participant/rounds/[^/]+/answers", path):
                diagnostic = AnswerDiagnostic("answer_post")
            elif method == "GET" and re.fullmatch(
                r"/api/v1/participant/answers/[^/]+/status", path
            ):
                diagnostic = AnswerDiagnostic("answer_status")
        diagnostic_token = current_answer_diagnostic.set(diagnostic)
        entry_exception = sys.exception()
        self._metrics.begin_http_request(method)

        async def observe_start(message: Message) -> None:
            nonlocal status
            candidate: object = None
            if message["type"] == "http.response.start":
                candidate = message.get("status")
                if diagnostic is not None:
                    diagnostic.emit(
                        "response_started",
                        http_status=candidate
                        if isinstance(candidate, int)
                        and not isinstance(candidate, bool)
                        and 100 <= candidate <= 599
                        else None,
                    )
                    message = {
                        **message,
                        "headers": [*message.get("headers", []), *diagnostic.response_headers()],
                    }
                if timing is not None:
                    timing.finish_response()
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (b"server-timing", timing.server_timing()),
                        ],
                    }
            await send(message)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                status = candidate

        try:
            if diagnostic is not None:
                diagnostic.emit("request_started")
            await self._app(scope, receive, observe_start)
        finally:
            if timing is not None:
                timing.finish_response()
            current_request_timing.reset(timing_token)
            active_exception = sys.exception()
            if active_exception is not None and active_exception is not entry_exception:
                record_answer_failure_stack(active_exception, "request_finished")
            current_answer_diagnostic.reset(diagnostic_token)
            if diagnostic is not None:
                diagnostic.emit(
                    "request_finished",
                    http_status=status,
                    reason=diagnostic_reason(active_exception)
                    if active_exception is not None and active_exception is not entry_exception
                    else None,
                )
            status_class = _status_class(status)
            has_new_failure = (
                active_exception is not None and active_exception is not entry_exception
            )
            if has_new_failure and not isinstance(active_exception, asyncio.CancelledError):
                status_class = "5xx"
            self._metrics.finish_http_request(
                method=method,
                route=_route_template(scope),
                status_class=status_class,
                duration_seconds=max(0.0, monotonic() - started_at),
            )
            if timing is not None:
                if timing.answer_difference_band is not None:
                    self._metrics.record_answer_difference(timing.answer_difference_band)
                self._metrics.record_participant_database_work(
                    method=method,
                    route=_route_template(scope),
                    counts=timing.database_work.copy(),
                )
                for stage, duration_seconds in timing.durations.copy().items():
                    self._metrics.record_participant_request_stage(
                        method=method,
                        route=_route_template(scope),
                        stage=stage,
                        duration_seconds=duration_seconds,
                    )

    async def _observe_websocket(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        audience = _websocket_audience(scope)
        is_active = False
        is_closed = False
        self._metrics.record_websocket_attempt(audience)

        def record_close(code: int | None, default_outcome: str) -> None:
            nonlocal is_closed
            if is_closed:
                return
            outcome = _CLOSE_OUTCOMES.get(code, default_outcome)
            self._metrics.record_websocket_close(
                audience=audience,
                outcome=outcome,
                code_class=_websocket_code_class(code),
            )
            is_closed = True

        async def observe_receive() -> Message:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                candidate = message.get("code")
                code = (
                    candidate
                    if isinstance(candidate, int) and not isinstance(candidate, bool)
                    else None
                )
                record_close(code, "client_disconnect")
            return message

        async def observe_send(message: Message) -> None:
            nonlocal is_active
            await send(message)
            if message["type"] == "websocket.accept" and not is_active:
                self._metrics.change_websocket_active(audience=audience, delta=1)
                is_active = True
            elif message["type"] == "websocket.close":
                candidate = message.get("code")
                code = (
                    candidate
                    if isinstance(candidate, int) and not isinstance(candidate, bool)
                    else None
                )
                record_close(code, "server_close")

        try:
            await self._app(scope, observe_receive, observe_send)
        finally:
            if is_active:
                self._metrics.change_websocket_active(audience=audience, delta=-1)
            record_close(None, "application_exit")
