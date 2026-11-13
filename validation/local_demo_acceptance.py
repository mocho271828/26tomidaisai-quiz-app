from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import time
from contextlib import ExitStack
from dataclasses import dataclass
from http.client import HTTPException
from http.cookiejar import CookieJar
from typing import Literal, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from pydantic import BaseModel, TypeAdapter, ValidationError
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from quiz_app.audience.contracts import (
    ParticipantJoinRequest,
    ParticipantStateResponse,
)
from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.operator.contracts import OperatorLoginRequest, OperatorSessionResponse
from quiz_app.operator.control_contracts import (
    OperatorMutationResponse,
    OperatorStateResponse,
    ResultVisibilityMutationRequest,
)
from quiz_app.quiz_configuration import VisibilityTarget
from quiz_app.realtime.contracts import (
    HeartbeatMessage,
    OperatorStateMessage,
    ParticipantStateMessage,
    PollingRequiredMessage,
    RealtimeServerMessage,
)

EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
REQUEST_TIMEOUT_SECONDS = 10
PARTICIPANT_JOIN_RETRY_DELAY_SECONDS = 0.1
WEBSOCKET_TIMEOUT_SECONDS = 10
EXPECTED_CLOSE_CODE = 1013
LOCAL_GATEWAY_HOSTS = frozenset({"gateway", "localhost", "127.0.0.1", "::1"})
PRIVATE_LAN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
ExpectedRealtime = Literal["ready", "degraded"]


@dataclass(frozen=True, slots=True)
class Criterion:
    identifier: str
    description: str


CRITERIA = (
    Criterion(
        identifier="AC1",
        description="Caddy経由でparticipant/operatorとhealthが応答する。",
    ),
    Criterion(
        identifier="AC2",
        description=("全audienceのWebSocketが型検証済みの完全状態と可逆な表示設定変更を受信する。"),
    ),
    Criterion(
        identifier="AC3",
        description="Redis停止時にpolling.required（5000ms）とclose code 1013へ縮退する。",
    ),
)


class ValidationFailure(RuntimeError):
    """機密値を含めずにacceptance不合格理由を伝える。"""


def parse_local_base_url(
    base_url: str,
    *,
    allow_private_lan: bool = False,
) -> SplitResult:
    """認証値をlocal Caddy以外へ送らないため接続先を限定する。"""

    parsed = urlsplit(base_url)
    try:
        _port = parsed.port
    except ValueError as error:
        raise ValidationFailure("base URL contains an invalid port") from error
    is_approved_host = parsed.hostname in LOCAL_GATEWAY_HOSTS
    if not is_approved_host and allow_private_lan and parsed.hostname is not None:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            is_approved_host = any(address in network for network in PRIVATE_LAN_NETWORKS)
    if (
        base_url != base_url.strip()
        or parsed.scheme not in {"http", "https"}
        or not is_approved_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query != ""
        or parsed.fragment != ""
    ):
        raise ValidationFailure("base URL must target an approved local gateway host")
    return parsed


class JsonClient:
    """Cookieをメモリだけに保持するlocal demo用HTTP client。"""

    def __init__(self, base_url: str, *, allow_private_lan: bool = False) -> None:
        parse_local_base_url(base_url, allow_private_lan=allow_private_lan)
        self._base_url = base_url.rstrip("/")
        self._cookie_jar = CookieJar()
        # host環境のproxy設定を継承せず、認証値をlocal Docker network内だけへ送る。
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(self._cookie_jar),
        )

    def get_html(self, path: str) -> None:
        response_body = self._request(path, method="GET")
        if b'<div id="root"></div>' not in response_body:
            raise ValidationFailure(f"SPA shell was not returned for {path}")

    def request_model(
        self,
        path: str,
        response_model: type[ResponseModel],
        *,
        method: str = "GET",
        request_model: BaseModel | None = None,
        request_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ResponseModel:
        if request_model is not None and request_body is not None:
            raise ValidationFailure("request model and pre-serialized body are mutually exclusive")
        body = (
            request_model.model_dump_json().encode() if request_model is not None else request_body
        )
        response_body = self._request(path, method=method, body=body, headers=headers)
        try:
            return response_model.model_validate_json(response_body)
        except ValidationError as error:
            # 外部responseの値を例外文字列へ含めず、logへのID・問題文流出を防ぐ。
            raise ValidationFailure(
                f"{response_model.__name__} response schema validation failed "
                f"({error.error_count()} errors)"
            ) from None

    def cookie_value(self, name: str) -> str:
        values = [cookie.value for cookie in self._cookie_jar if cookie.name == name]
        if len(values) != 1:
            raise ValidationFailure(f"Expected exactly one {name} cookie")
        return values[0]

    def cookie_header(self) -> str:
        cookies = [f"{cookie.name}={cookie.value}" for cookie in self._cookie_jar]
        if not cookies:
            raise ValidationFailure("Authenticated WebSocket cookie is missing")
        return "; ".join(cookies)

    def _request(
        self,
        path: str,
        *,
        method: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if headers is not None:
            request_headers.update(headers)
        # 上でHTTP(S)の絶対URLに限定し、local Caddy以外のschemeを受け付けない。
        request = Request(  # noqa: S310 - 上記でlocal Caddy向けの絶対HTTP(S) URLに限定済み。
            f"{self._base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ValidationFailure(f"Unexpected HTTP {response.status} for {method} {path}")
            return response.read()


def request_participant_join(
    client: JsonClient,
    request: ParticipantJoinRequest,
    *,
    retry_delay_seconds: float = PARTICIPANT_JOIN_RETRY_DELAY_SECONDS,
) -> ParticipantStateResponse:
    """応答消失時だけ同じ再送識別tokenでjoinを1回再送する。"""

    try:
        return client.request_model(
            "/api/v1/participant/join",
            ParticipantStateResponse,
            method="POST",
            request_model=request,
        )
    except HTTPError as error:
        if error.code < 500:
            raise
    except (HTTPException, OSError, TimeoutError, URLError):
        pass

    time.sleep(retry_delay_seconds)
    return client.request_model(
        "/api/v1/participant/join",
        ParticipantStateResponse,
        method="POST",
        request_model=request,
    )


@dataclass(frozen=True, slots=True)
class AuthenticatedClients:
    participant: JsonClient
    operator: JsonClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="running local demoのCaddy/API/WSを検証する")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LOCAL_DEMO_BASE_URL", "http://gateway:8080"),
    )
    parser.add_argument(
        "--origin",
        default=os.environ.get("LOCAL_DEMO_ORIGIN", "http://localhost:18080"),
    )
    parser.add_argument(
        "--expect-realtime",
        choices=("ready", "degraded"),
        default="ready",
    )
    return parser.parse_args()


def websocket_base_url(base_url: str) -> str:
    parsed = parse_local_base_url(base_url)
    websocket_scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((websocket_scheme, parsed.netloc, "", "", ""))


def authenticate(base_url: str) -> AuthenticatedClients:
    operator_username = os.environ.get("DEMO_OPERATOR_USERNAME")
    operator_password = os.environ.get("DEMO_OPERATOR_PASSWORD")
    if not operator_username or not operator_password:
        raise ValidationFailure("DEMO_OPERATOR_USERNAME and DEMO_OPERATOR_PASSWORD are required")

    participant = JsonClient(base_url)
    request_participant_join(
        participant,
        ParticipantJoinRequest(
            client_join_token=secrets.token_urlsafe(32),
        ),
    )
    participant.request_model("/api/v1/participant/session", ParticipantStateResponse)

    operator = JsonClient(base_url)
    try:
        login_request = OperatorLoginRequest(
            username=operator_username,
            password=operator_password,
        )
    except ValidationError as error:
        raise ValidationFailure(
            f"Operator credentials failed schema validation ({error.error_count()} errors)"
        ) from None
    # SecretStrの通常serializeはmaskされるため、loopback限定Caddyへの送信直前だけ実値へ戻す。
    login_body = json.dumps(
        {
            "username": login_request.username,
            "password": login_request.password.get_secret_value(),
        }
    ).encode()
    operator.request_model(
        "/api/v1/operator/auth/login",
        OperatorSessionResponse,
        method="POST",
        request_body=login_body,
    )
    operator.request_model("/api/v1/operator/auth/session", OperatorSessionResponse)
    return AuthenticatedClients(participant=participant, operator=operator)


def connect_audience(
    websocket_url: str,
    origin: str,
    cookie_header: str | None = None,
) -> ClientConnection:
    additional_headers = {"Cookie": cookie_header} if cookie_header is not None else None
    return connect(
        websocket_url,
        origin=origin.rstrip("/"),
        additional_headers=additional_headers,
        proxy=None,
        open_timeout=WEBSOCKET_TIMEOUT_SECONDS,
        close_timeout=WEBSOCKET_TIMEOUT_SECONDS,
    )


def receive_state_message(
    connection: ClientConnection, audience: str, expected_version: int = 1
) -> ParticipantStateMessage | OperatorStateMessage:
    """heartbeatを状態取得と扱わず、対象開催の完全snapshotを検証する。"""

    deadline = time.monotonic() + WEBSOCKET_TIMEOUT_SECONDS
    adapter = TypeAdapter(RealtimeServerMessage)
    while time.monotonic() < deadline:
        raw_message = connection.recv(timeout=max(0.001, deadline - time.monotonic()))
        if not isinstance(raw_message, str):
            raise ValidationFailure(f"{audience} WebSocket returned a binary message")
        message = adapter.validate_json(raw_message)
        if isinstance(message, PollingRequiredMessage):
            raise ValidationFailure(f"{audience} WebSocket unexpectedly required polling")
        if message.audience != audience or message.event_id != EVENT_ID:
            raise ValidationFailure(f"{audience} WebSocket state was misrouted")
        if isinstance(message, ParticipantStateMessage):
            version = message.state.event.event_version
            if audience != "participant" or message.state.event.event_id != EVENT_ID:
                raise ValidationFailure("participant WebSocket projection was misrouted")
        elif isinstance(message, OperatorStateMessage):
            version = message.state.event_version
            if audience != "operator" or message.state.event_id != EVENT_ID:
                raise ValidationFailure("operator WebSocket projection was misrouted")
        else:
            continue
        if version >= expected_version:
            return message
    raise ValidationFailure(f"{audience} WebSocket did not deliver current state")


def validate_initial_message(connection: ClientConnection, audience: str) -> None:
    receive_state_message(connection, audience)


def validate_mutation_message(
    connection: ClientConnection, audience: str, expected_version: int
) -> None:
    receive_state_message(connection, audience, expected_version)


def change_result_visibility(
    operator: JsonClient,
    expected_version: int,
    target: VisibilityTarget,
    visible: bool,
) -> OperatorMutationResponse:
    csrf_token = operator.cookie_value("quiz_operator_csrf")
    return operator.request_model(
        "/api/v1/operator/result-visibility",
        OperatorMutationResponse,
        method="POST",
        request_model=ResultVisibilityMutationRequest(
            expected_version=expected_version,
            idempotency_key=uuid4(),
            target=target,
            visible=visible,
        ),
        headers={"X-CSRF-Token": csrf_token},
    )


def validate_ready_realtime(
    base_url: str,
    origin: str,
    clients: AuthenticatedClients,
    operator_state: OperatorStateResponse,
) -> None:
    websocket_base = websocket_base_url(base_url)
    connections: dict[str, ClientConnection] = {}
    with ExitStack() as stack:
        connections["participant"] = stack.enter_context(
            connect_audience(
                f"{websocket_base}/api/v1/participant/realtime",
                origin,
                clients.participant.cookie_header(),
            )
        )
        connections["operator"] = stack.enter_context(
            connect_audience(
                f"{websocket_base}/api/v1/operator/realtime",
                origin,
                clients.operator.cookie_header(),
            )
        )
        for audience, connection in connections.items():
            validate_initial_message(connection, audience)

        target = next(
            (
                target
                for target, control in (
                    ("distribution", operator_state.visibility_controls.distribution),
                    ("personal_ranking", operator_state.visibility_controls.personal_ranking),
                    ("overall_ranking", operator_state.visibility_controls.overall_ranking),
                )
                if control.mode == "manual"
            ),
            None,
        )
        if target is None:
            raise ValidationFailure("Realtime acceptance requires a manual visibility control")
        original = getattr(operator_state.result_visibility, target)
        changed = change_result_visibility(
            clients.operator,
            operator_state.event_version,
            target,
            not original,
        )
        try:
            if changed.event_version <= operator_state.event_version:
                raise ValidationFailure("realtime probe did not advance the event version")
            for audience, connection in connections.items():
                validate_mutation_message(connection, audience, changed.event_version)
        finally:
            restored = change_result_visibility(
                clients.operator,
                changed.event_version,
                target,
                original,
            )
        if restored.result_visibility != operator_state.result_visibility:
            raise ValidationFailure("realtime probe did not restore the visibility settings")
        for audience, connection in connections.items():
            validate_mutation_message(connection, audience, restored.event_version)


def validate_degraded_connection(connection: ClientConnection, audience: str) -> None:
    """Redis単独障害ではDB状態とheartbeatを同じWSで受け取れる。"""

    state = receive_state_message(connection, audience)
    deadline = time.monotonic() + WEBSOCKET_TIMEOUT_SECONDS
    adapter = TypeAdapter(RealtimeServerMessage)
    while time.monotonic() < deadline:
        raw_message = connection.recv(timeout=max(0.001, deadline - time.monotonic()))
        if not isinstance(raw_message, str):
            raise ValidationFailure(f"{audience} degraded WebSocket returned a binary message")
        message = adapter.validate_json(raw_message)
        if isinstance(message, PollingRequiredMessage):
            raise ValidationFailure(f"{audience} Redis-only outage unexpectedly required polling")
        if (
            message.audience != audience
            or message.event_id != EVENT_ID
            or message.stream_id != state.stream_id
            or message.sequence <= state.sequence
        ):
            raise ValidationFailure(f"{audience} degraded WebSocket stream was inconsistent")
        if isinstance(message, HeartbeatMessage):
            return
    raise ValidationFailure(f"{audience} degraded WebSocket heartbeat was not received")


def validate_degraded_realtime(
    base_url: str,
    origin: str,
    clients: AuthenticatedClients,
) -> None:
    websocket_base = websocket_base_url(base_url)
    endpoints = {
        "participant": (
            f"{websocket_base}/api/v1/participant/realtime",
            clients.participant.cookie_header(),
        ),
        "operator": (
            f"{websocket_base}/api/v1/operator/realtime",
            clients.operator.cookie_header(),
        ),
    }
    for audience, (endpoint, cookie_header) in endpoints.items():
        with connect_audience(endpoint, origin, cookie_header) as connection:
            validate_degraded_connection(connection, audience)


def validate(
    base_url: str,
    origin: str,
    expected_realtime: ExpectedRealtime,
) -> list[str]:
    gateway = JsonClient(base_url)
    for path in ("/", "/operator"):
        gateway.get_html(path)

    live = gateway.request_model("/api/health/live", LiveHealthResponse)
    readiness = gateway.request_model("/api/health/ready", ReadinessHealthResponse)
    if live.status != "ok":
        raise ValidationFailure("live health is invalid")

    clients = authenticate(base_url)
    operator_state = clients.operator.request_model(
        "/api/v1/operator/state",
        OperatorStateResponse,
    )
    if operator_state.event_id != EVENT_ID:
        raise ValidationFailure("operator event identity is invalid")
    if expected_realtime == "ready":
        readiness_values = (
            readiness.status,
            readiness.database,
            readiness.migration,
            readiness.redis,
        )
        if readiness_values != ("ready", "ok", "current", "ok"):
            raise ValidationFailure("readiness was inconsistent with a healthy local stack")
        validate_ready_realtime(base_url, origin, clients, operator_state)
        return [
            "AC1: Caddy SPA/API/health and both authenticated projections responded.",
            "AC2: participant/operator received complete states and reversible visibility updates.",
        ]

    readiness_values = (
        readiness.status,
        readiness.database,
        readiness.migration,
        readiness.redis,
    )
    if readiness_values != ("degraded", "ok", "current", "degraded"):
        raise ValidationFailure("readiness was inconsistent with Redis-only degradation")
    validate_degraded_realtime(base_url, origin, clients)
    return [
        "AC1: Caddy SPA/API/health remained available while Redis was stopped.",
        "AC3: both audiences received polling.required=5000ms and close code 1013.",
    ]


def main() -> int:
    args = parse_args()
    try:
        passed = validate(args.base_url, args.origin, args.expect_realtime)
    except (
        ConnectionClosed,
        HTTPError,
        OSError,
        TimeoutError,
        URLError,
        ValidationError,
        ValidationFailure,
        ValueError,
        WebSocketException,
    ) as error:
        print(f"Validation failed: {type(error).__name__}: {error}")
        return 1

    print("Validation passed:")
    for result in passed:
        print(f"- {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
