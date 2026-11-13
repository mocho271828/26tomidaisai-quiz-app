import json
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from quiz_app.load_test import controller_http as http_module
from quiz_app.load_test.controller_contracts import (
    ControllerApiMutationError,
    ControllerOperation,
)
from quiz_app.load_test.controller_http import (
    MAXIMUM_RESPONSE_BYTES,
    MUTATION_TIMEOUT_SECONDS,
    OPERATOR_CSRF_COOKIE,
    OPERATOR_SESSION_COOKIE,
    READ_TIMEOUT_SECONDS,
    ControllerHttpClient,
    ControllerHttpError,
    ControllerHttpResponseError,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.operator.control_contracts import OperatorMutationRequest

ORIGIN = "https://quiz.aws.internal"
EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")
ROUND_ID = UUID("70000000-0000-4000-8000-000000000002")
IDEMPOTENCY_KEY = UUID("70000000-0000-4000-8000-000000000003")
ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000004")
NEXT_ROUND_ID = UUID("70000000-0000-4000-8000-000000000005")
PASSWORD_SENTINEL = "DO-NOT-RETAIN-CONTROLLER-PASSWORD"  # noqa: S105 - 漏えい検査用の番兵値


class StubResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str | None = "application/json",
        final_url: str | None = None,
        set_cookies: tuple[str, ...] = (),
        content_length: str | None = None,
        content_encoding: str | None = None,
    ) -> None:
        self._body = body
        self._status = status
        self._final_url = final_url
        self.headers = Message()
        if content_type is not None:
            self.headers.add_header("Content-Type", content_type)
        if content_length is None:
            content_length = str(len(body))
        if content_length:
            self.headers.add_header("Content-Length", content_length)
        if content_encoding is not None:
            self.headers.add_header("Content-Encoding", content_encoding)
        for cookie in set_cookies:
            self.headers.add_header("Set-Cookie", cookie)
        self.is_closed = False

    def getcode(self) -> int:
        return self._status

    def geturl(self) -> str:
        if self._final_url is None:
            raise AssertionError("test response URL must be assigned by FakeOpener")
        return self._final_url

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def close(self) -> None:
        self.is_closed = True


Responder = Callable[[Request, float, CookieJar], StubResponse]


class FakeOpener:
    def __init__(self, cookie_jar: CookieJar, responder: Responder) -> None:
        self._cookie_jar = cookie_jar
        self._responder = responder
        self.requests: list[tuple[str, str, bytes | None, dict[str, str], float]] = []

    def open(self, request: Request, *, timeout: float) -> StubResponse:
        headers = {name.lower(): value for name, value in request.header_items()}
        self.requests.append((request.full_url, request.method, request.data, headers, timeout))
        response = self._responder(request, timeout, self._cookie_jar)
        if response._final_url is None:
            response._final_url = request.full_url
        return response


def _manifest(public_origin: str = ORIGIN) -> LoadTestManifest:
    return LoadTestManifest.model_construct(
        public_origin=public_origin,
        event=SimpleNamespace(id=EVENT_ID),
    )


def _json_response(payload: object, **kwargs: object) -> StubResponse:
    return StubResponse(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(),
        **kwargs,
    )


def _session_payload(account_id: UUID = ACCOUNT_ID) -> dict[str, object]:
    return {
        "operator": {
            "account_id": str(account_id),
            "username": "load-operator",
            "role": "Operator",
        }
    }


def _mutation_payload(operation: str, round_id: UUID = ROUND_ID) -> dict[str, object]:
    return {
        "operation": operation,
        "event_id": str(EVENT_ID),
        "event_version": 2,
        "round_id": str(round_id),
        "round_version": 2,
        "round_state": "PreparingAssets",
        "result_visibility": {
            "distribution": True,
            "personal_ranking": False,
            "overall_ranking": False,
        },
        "open_at": None,
        "standard_close_at": None,
        "valid_answer_count": None,
        "ranking_row_count": None,
        "is_replay": False,
    }


def _state_payload() -> dict[str, object]:
    return {
        "award_ceremony_phase": None,
        "event_id": str(EVENT_ID),
        "projected_at": "2026-09-08T10:00:00Z",
        "event_title": "[LOAD-TEST] event",
        "event_state": "LobbyOpen",
        "event_version": 1,
        "result_visibility": {
            "distribution": True,
            "personal_ranking": False,
            "overall_ranking": False,
        },
        "start_delay_seconds": 5,
        "visibility_controls": {
            target: {"mode": "manual", "initially_visible": visible, "changes": ()}
            for target, visible in (
                ("distribution", True),
                ("personal_ranking", False),
                ("overall_ranking", False),
            )
        },
        "participant_count": 600,
        "available_questions": [],
        "used_question_ids": [],
        "current_round": None,
        "latest_ranking_snapshot_version": None,
    }


def _ranking_payload() -> dict[str, object]:
    return {
        "snapshot_version": 1,
        "round_id": str(ROUND_ID),
        "rows": [
            {
                "overall_rank": 1,
                "public_label": "AAA",
                "total_points": 100,
                "correct_count": 1,
            }
        ],
    }


def _set_cookie(
    cookie_jar: CookieJar,
    name: str,
    value: str,
    path: str,
) -> None:
    cookie_jar.set_cookie(
        Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain="quiz.aws.internal",
            domain_specified=False,
            domain_initial_dot=False,
            path=path,
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )


def _login_response(
    cookie_jar: CookieJar,
    *,
    session: str = "session-one",
    csrf: str = "csrf-one",
    headers: tuple[str, str] | None = None,
    account_id: UUID = ACCOUNT_ID,
) -> StubResponse:
    _set_cookie(cookie_jar, OPERATOR_SESSION_COOKIE, session, "/api/v1/operator")
    _set_cookie(cookie_jar, OPERATOR_CSRF_COOKIE, csrf, "/")
    set_cookie_headers = headers or (
        (
            f"{OPERATOR_SESSION_COOKIE}={session}; Path=/api/v1/operator; Secure; "
            "HttpOnly; SameSite=strict"
        ),
        f"{OPERATOR_CSRF_COOKIE}={csrf}; Path=/; Secure; SameSite=strict",
    )
    return _json_response(_session_payload(account_id), set_cookies=set_cookie_headers)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    responder: Responder,
    *,
    reauthenticate: Callable[[ControllerHttpClient], None] | None = None,
    public_origin: str = ORIGIN,
) -> tuple[ControllerHttpClient, FakeOpener]:
    created: list[FakeOpener] = []

    def factory(cookie_jar: CookieJar, _ca_file: Path | None = None) -> FakeOpener:
        opener = FakeOpener(cookie_jar, responder)
        created.append(opener)
        return opener

    monkeypatch.setattr(http_module, "_build_secure_opener", factory)
    client = ControllerHttpClient(
        _manifest(public_origin),
        reauthenticate=reauthenticate,
    )
    return client, created[0]


@pytest.mark.parametrize(
    "origin",
    [
        "http://quiz.aws.internal",
        "https://quiz.aws.internal/",
        "https://user@quiz.aws.internal",
        "https://quiz.aws.internal/path",
        "https://quiz.aws.internal?query=1",
        "https://127.0.0.1",
    ],
)
def test_origin_must_be_manifest_canonical_remote_https(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    monkeypatch.setattr(
        http_module,
        "_build_secure_opener",
        lambda _: pytest.fail("invalid origin must fail before creating an opener"),
    )
    with pytest.raises(ControllerHttpError, match="controller_http_origin_invalid"):
        ControllerHttpClient(_manifest(origin))


def test_manifest_event_id_must_be_uuid_before_creating_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = LoadTestManifest.model_construct(
        public_origin=ORIGIN,
        event=SimpleNamespace(id="../../health/live"),
    )
    monkeypatch.setattr(
        http_module,
        "_build_secure_opener",
        lambda _: pytest.fail("invalid manifest must fail before creating an opener"),
    )

    with pytest.raises(ControllerHttpError, match="controller_http_manifest_invalid"):
        ControllerHttpClient(manifest)


def test_secure_opener_disables_proxy_rejects_redirects_and_verifies_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: tuple[object, ...] = ()

    def capture_handlers(*handlers: object) -> object:
        nonlocal captured_handlers
        captured_handlers = handlers
        return object()

    monkeypatch.setattr(http_module, "build_opener", capture_handlers)
    http_module._build_secure_opener(CookieJar())
    proxy = next(handler for handler in captured_handlers if isinstance(handler, ProxyHandler))
    https = next(handler for handler in captured_handlers if isinstance(handler, HTTPSHandler))
    assert proxy.proxies == {}
    assert any(isinstance(handler, http_module._RejectRedirects) for handler in captured_handlers)
    assert https._context.check_hostname is True
    assert https._context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ControllerHttpError, match="controller_http_redirect_refused"):
        http_module._RejectRedirects().redirect_request(
            Request(ORIGIN),
            None,
            307,
            "redirect",
            Message(),
            f"{ORIGIN}/other",
        )


def test_only_typed_fixed_routes_are_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: Request, _: float, __: CookieJar) -> StubResponse:
        if request.full_url.endswith("/api/v1/time"):
            return _json_response(
                {"server_time": "2026-09-08T10:00:00Z", "shared_sample_age_ms": 0}
            )
        if request.full_url.endswith("/api/health/ready"):
            return _json_response(
                {
                    "status": "ready",
                    "database": "ok",
                    "migration": "current",
                    "redis": "ok",
                }
            )
        if request.full_url.endswith("/operator/state"):
            return _json_response(_state_payload())
        if request.full_url.endswith("/operator/ranking"):
            return _json_response(_ranking_payload())
        raise AssertionError(request.full_url)

    client, opener = _client(monkeypatch, respond)

    client.get_server_time()
    client.get_readiness()
    client.get_event_state()
    ranking = client.get_latest_ranking()

    assert ranking is not None
    assert ranking.round_id == ROUND_ID
    assert ranking.snapshot_version == 1
    assert len(ranking.rows) == 1
    assert [request[0] for request in opener.requests] == [
        f"{ORIGIN}/api/v1/time",
        f"{ORIGIN}/api/health/ready",
        f"{ORIGIN}/api/v1/operator/state",
        f"{ORIGIN}/api/v1/operator/ranking",
    ]
    assert all(request[1] == "GET" for request in opener.requests)
    assert all(request[4] == READ_TIMEOUT_SECONDS for request in opener.requests)


def test_core_api_methods_keep_exact_mutation_bytes_and_add_live_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_body = (
        b'{ "expected_version": 1, "idempotency_key": "70000000-0000-4000-8000-000000000003" }'
    )

    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            return _login_response(cookie_jar)
        if request.full_url.endswith("/api/health/live"):
            return _json_response({"status": "ok", "service": "quiz-api", "version": "a" * 40})
        if request.full_url.endswith("/operator/state"):
            return _json_response(_state_payload())
        if request.full_url.endswith("/prepare"):
            return _json_response(_mutation_payload("begin_preparation"))
        raise AssertionError(request.full_url)

    client, opener = _client(monkeypatch, respond)
    client.authenticate("load-operator", SecretStr(PASSWORD_SENTINEL))

    assert client.get_liveness().version == "a" * 40
    assert client.get_live_health().version == "a" * 40
    assert client.get_event_state().event_id == EVENT_ID
    assert (
        client.post_mutation("begin_preparation", ROUND_ID, exact_body).operation
        == "begin_preparation"
    )

    sent = next(request for request in opener.requests if request[0].endswith("/prepare"))
    assert sent[2] == exact_body
    assert sent[4] == MUTATION_TIMEOUT_SECONDS


def test_event_state_rejects_non_manifest_event_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {**_state_payload(), "event_id": str(UUID(int=99))}
    client, opener = _client(monkeypatch, lambda *_: _json_response(payload))

    with pytest.raises(ControllerHttpError, match="controller_http_response_identity_mismatch"):
        client.get_event_state()
    assert [request[0] for request in opener.requests] == [f"{ORIGIN}/api/v1/operator/state"]


def test_latest_ranking_accepts_null_before_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(monkeypatch, lambda *_: _json_response(None))

    assert client.get_latest_ranking() is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_ranking_payload(), "snapshot_version": "1"},
        {**_ranking_payload(), "round_id": "invalid"},
        {**_ranking_payload(), "unknown": "value"},
        {**_ranking_payload(), "rows": [{"overall_rank": 1}]},
    ],
)
def test_latest_ranking_rejects_invalid_response_schema(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    client, _ = _client(monkeypatch, lambda *_: _json_response(payload))

    with pytest.raises(ControllerHttpError, match="controller_http_response_schema_invalid"):
        client.get_latest_ranking()


def test_post_mutation_rejects_unknown_operation_and_unbounded_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, opener = _client(monkeypatch, lambda *_: pytest.fail("must not use network"))

    with pytest.raises(ControllerHttpError, match="controller_http_operation_invalid"):
        client.post_mutation(
            cast(ControllerOperation, "set_ranking_visibility"),
            ROUND_ID,
            b"{}",
        )
    with pytest.raises(ControllerHttpError, match="controller_http_mutation_request_invalid"):
        client.post_mutation("begin_preparation", ROUND_ID, b"x" * 10_001)
    with pytest.raises(ControllerHttpError, match="controller_http_mutation_request_invalid"):
        client.post_mutation(
            "begin_preparation",
            ROUND_ID,
            b'{"expected_version":1,"expected_version":2}',
        )
    with pytest.raises(ControllerHttpError, match="controller_http_round_id_invalid"):
        client.post_mutation(
            "begin_preparation",
            cast(UUID, "../../auth/logout"),
            (b'{"expected_version":1,"idempotency_key":"70000000-0000-4000-8000-000000000003"}'),
        )
    assert opener.requests == []


@pytest.mark.parametrize(
    ("operation", "path_suffix"),
    [
        ("begin_preparation", "prepare"),
        ("schedule_round", "schedule"),
        ("advance_round", "advance"),
        ("complete_event", "complete-event"),
    ],
)
def test_post_mutation_uses_only_fixed_operation_routes(
    monkeypatch: pytest.MonkeyPatch,
    operation: ControllerOperation,
    path_suffix: str,
) -> None:
    body_payload: dict[str, object] = {
        "expected_version": 1,
        "idempotency_key": str(IDEMPOTENCY_KEY),
    }
    response_round_id = ROUND_ID
    if operation == "advance_round":
        body_payload["question_id"] = str(NEXT_ROUND_ID)
        response_round_id = NEXT_ROUND_ID
    if operation == "schedule_round":
        body_payload["execute_not_after"] = "2026-09-03T01:14:56Z"
        body_payload["target_open_at"] = "2026-09-03T01:15:00Z"
    body = json.dumps(body_payload, separators=(",", ":")).encode()

    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            return _login_response(cookie_jar)
        return _json_response(_mutation_payload(operation, response_round_id))

    client, opener = _client(monkeypatch, respond)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))

    assert client.post_mutation(operation, ROUND_ID, body).operation == operation
    assert opener.requests[-1][0] == (f"{ORIGIN}/api/v1/operator/rounds/{ROUND_ID}/{path_suffix}")
    assert opener.requests[-1][2] == body


def test_post_mutation_refuses_unknown_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            return _login_response(cookie_jar)
        raise AssertionError("未知のoperationでrouteへ接続してはならない")

    client, opener = _client(monkeypatch, respond)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))
    request_count_after_login = len(opener.requests)

    with pytest.raises(ControllerHttpError, match="controller_http_operation_invalid"):
        client.post_mutation(
            cast(ControllerOperation, "unknown_operation"),
            ROUND_ID,
            OperatorMutationRequest(
                expected_version=1,
                idempotency_key=IDEMPOTENCY_KEY,
            )
            .model_dump_json()
            .encode("utf-8"),
        )

    assert len(opener.requests) == request_count_after_login


def test_login_validates_cookies_and_mutation_uses_jar_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: Request, timeout: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            assert timeout == READ_TIMEOUT_SECONDS
            return _login_response(cookie_jar)
        assert request.full_url == f"{ORIGIN}/api/v1/operator/rounds/{ROUND_ID}/prepare"
        assert timeout == MUTATION_TIMEOUT_SECONDS
        return _json_response(_mutation_payload("begin_preparation"))

    client, opener = _client(monkeypatch, respond)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))
    result = client.prepare_round(
        ROUND_ID,
        OperatorMutationRequest(
            expected_version=1,
            idempotency_key=IDEMPOTENCY_KEY,
        ),
    )

    assert result.operation == "begin_preparation"
    assert opener.requests[-1][3]["x-csrf-token"] == "csrf-one"
    assert not hasattr(client, "_password")
    assert PASSWORD_SENTINEL not in repr(client.__dict__)


@pytest.mark.parametrize(
    "cookie_headers",
    [
        (
            f"{OPERATOR_SESSION_COOKIE}=s; Path=/api/v1/operator; HttpOnly; SameSite=strict",
            f"{OPERATOR_CSRF_COOKIE}=c; Path=/; Secure; SameSite=strict",
        ),
        (
            f"{OPERATOR_SESSION_COOKIE}=s; Path=/; Secure; HttpOnly; SameSite=strict",
            f"{OPERATOR_CSRF_COOKIE}=c; Path=/; Secure; SameSite=strict",
        ),
        (
            f"{OPERATOR_SESSION_COOKIE}=s; Path=/api/v1/operator; Secure; HttpOnly; SameSite=lax",
            f"{OPERATOR_CSRF_COOKIE}=c; Path=/; Secure; SameSite=strict",
        ),
        (
            (
                f"{OPERATOR_SESSION_COOKIE}=s; Path=/api/v1/operator; Secure; "
                "HttpOnly; SameSite=strict"
            ),
            f"{OPERATOR_CSRF_COOKIE}=c; Path=/; Secure; HttpOnly; SameSite=strict",
        ),
        (
            (
                f"{OPERATOR_SESSION_COOKIE}=s; Domain=quiz.aws.internal; "
                "Path=/api/v1/operator; Secure; HttpOnly; SameSite=strict"
            ),
            f"{OPERATOR_CSRF_COOKIE}=c; Path=/; Secure; SameSite=strict",
        ),
    ],
)
def test_login_rejects_cookie_attribute_drift_without_leaking_password(
    monkeypatch: pytest.MonkeyPatch,
    cookie_headers: tuple[str, str],
) -> None:
    def respond(_: Request, __: float, cookie_jar: CookieJar) -> StubResponse:
        return _login_response(cookie_jar, session="s", csrf="c", headers=cookie_headers)

    client, _ = _client(monkeypatch, respond)
    with pytest.raises(ControllerHttpError) as raised:
        client.login("load-operator", SecretStr(PASSWORD_SENTINEL))

    assert str(raised.value) == "controller_http_cookie_contract_invalid"
    assert PASSWORD_SENTINEL not in str(raised.value)
    assert list(client._cookie_jar) == []


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (
            StubResponse(b"{}", content_type="text/html"),
            "controller_http_response_content_type_invalid",
        ),
        (
            StubResponse(
                b'{"server_time":"2026-09-08T10:00:00Z","server_time":"2026-09-08T10:00:01Z",'
                b'"shared_sample_age_ms":0}'
            ),
            "controller_http_response_json_invalid",
        ),
        (
            StubResponse(b'{"server_time":1,"shared_sample_age_ms":0}'),
            "controller_http_response_schema_invalid",
        ),
        (
            StubResponse(b"x" * (MAXIMUM_RESPONSE_BYTES + 1)),
            "controller_http_response_too_large",
        ),
        (
            StubResponse(
                b'{"server_time":"2026-09-08T10:00:00Z","shared_sample_age_ms":0}',
                content_encoding="gzip",
            ),
            "controller_http_response_encoding_invalid",
        ),
    ],
)
def test_response_boundary_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    response: StubResponse,
    expected_code: str,
) -> None:
    client, _ = _client(monkeypatch, lambda *_: response)
    with pytest.raises(ControllerHttpError, match=expected_code):
        client.get_server_time()


def test_changed_final_url_is_rejected_before_body_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {"server_time": "2026-09-08T10:00:00Z", "shared_sample_age_ms": 0},
        final_url="https://other.aws.internal/api/v1/time",
    )
    client, _ = _client(monkeypatch, lambda *_: response)

    with pytest.raises(ControllerHttpError, match="controller_http_redirect_refused"):
        client.get_server_time()


def test_known_error_is_typed_and_unknown_code_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _json_response(
        {"code": "state_conflict", "message": "safe server message"},
        status=409,
    )
    client, _ = _client(monkeypatch, lambda *_: response)
    with pytest.raises(ControllerHttpResponseError) as raised:
        client.get_server_time()
    assert raised.value.status_code == 409
    assert raised.value.code == "state_conflict"

    invalid = _json_response(
        {"code": PASSWORD_SENTINEL, "message": PASSWORD_SENTINEL},
        status=409,
    )
    client, _ = _client(monkeypatch, lambda *_: invalid)
    with pytest.raises(ControllerHttpError) as rejected:
        client.get_server_time()
    assert str(rejected.value) == "controller_http_error_response_invalid"
    assert PASSWORD_SENTINEL not in str(rejected.value)


def test_round_backup_failure_maps_to_core_mutation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            return _login_response(cookie_jar)
        return _json_response(
            {"code": "round_backup_failed", "message": "safe failure"},
            status=503,
        )

    client, _ = _client(monkeypatch, respond)
    client.authenticate("load-operator", SecretStr(PASSWORD_SENTINEL))
    body = (
        OperatorMutationRequest(
            expected_version=1,
            idempotency_key=IDEMPOTENCY_KEY,
        )
        .model_dump_json()
        .encode()
    )

    with pytest.raises(ControllerApiMutationError, match="round_backup_failed"):
        client.post_mutation("complete_event", ROUND_ID, body)


def test_transport_exception_does_not_include_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_with_secret(_: Request, __: float, ___: CookieJar) -> StubResponse:
        raise URLError(PASSWORD_SENTINEL)

    client, _ = _client(monkeypatch, fail_with_secret)

    with pytest.raises(ControllerHttpError) as raised:
        client.authenticate("load-operator", SecretStr(PASSWORD_SENTINEL))
    assert str(raised.value) == "controller_http_transport_failed"
    assert PASSWORD_SENTINEL not in str(raised.value)


def test_401_reauthentication_rebuilds_csrf_but_preserves_mutation_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation_attempts = 0
    login_attempts = 0

    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        nonlocal login_attempts, mutation_attempts
        if request.full_url.endswith("/auth/login"):
            login_attempts += 1
            return _login_response(
                cookie_jar,
                session=f"session-{login_attempts}",
                csrf=f"csrf-{login_attempts}",
            )
        mutation_attempts += 1
        if mutation_attempts == 1:
            return _json_response(
                {
                    "code": "operator_authentication_required",
                    "message": "authenticate",
                },
                status=401,
            )
        return _json_response(_mutation_payload("begin_preparation"))

    callback_count = 0

    def reauthenticate(client: ControllerHttpClient) -> None:
        nonlocal callback_count
        callback_count += 1
        client.login("load-operator", SecretStr(PASSWORD_SENTINEL))

    client, opener = _client(monkeypatch, respond, reauthenticate=reauthenticate)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))
    request = OperatorMutationRequest(
        expected_version=1,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    client.prepare_round(ROUND_ID, request)

    mutation_requests = [item for item in opener.requests if item[0].endswith("/prepare")]
    assert callback_count == 1
    assert len(mutation_requests) == 2
    assert mutation_requests[0][2] == mutation_requests[1][2]
    assert mutation_requests[0][3]["x-csrf-token"] == "csrf-1"
    assert mutation_requests[1][3]["x-csrf-token"] == "csrf-2"


def test_401_reauthentication_rejects_operator_identity_change_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_attempts = 0
    mutation_attempts = 0

    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        nonlocal login_attempts, mutation_attempts
        if request.full_url.endswith("/auth/login"):
            login_attempts += 1
            return _login_response(
                cookie_jar,
                session=f"session-{login_attempts}",
                csrf=f"csrf-{login_attempts}",
                account_id=ACCOUNT_ID if login_attempts == 1 else UUID(int=99),
            )
        mutation_attempts += 1
        return _json_response(
            {"code": "operator_authentication_required", "message": "authenticate"},
            status=401,
        )

    def reauthenticate(client: ControllerHttpClient) -> None:
        client.login("load-operator", SecretStr(PASSWORD_SENTINEL))

    client, _ = _client(monkeypatch, respond, reauthenticate=reauthenticate)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))

    with pytest.raises(ControllerHttpError, match="controller_http_operator_identity_drift"):
        client.prepare_round(
            ROUND_ID,
            OperatorMutationRequest(
                expected_version=1,
                idempotency_key=IDEMPOTENCY_KEY,
            ),
        )
    assert mutation_attempts == 1


def test_204_logout_requires_empty_body_and_clears_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            return _login_response(cookie_jar)
        return StubResponse(b"", status=204, content_type=None, content_length="")

    client, _ = _client(monkeypatch, respond)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))
    client.logout()

    assert list(client._cookie_jar) == []


def test_module_does_not_offer_generic_url_or_insecure_configuration() -> None:
    public_methods = {name for name in dir(ControllerHttpClient) if not name.startswith("_")}
    assert "request" not in public_methods
    assert "open" not in public_methods
    assert "set_base_url" not in public_methods
    assert "set_tls_context" not in public_methods
    assert "set_proxy" not in public_methods
    assert Path(http_module.__file__).name == "controller_http.py"


def _run_ca(tmp_path: Path) -> Path:
    directory = tmp_path / "tls"
    directory.mkdir(mode=0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "load-test-run-ca")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = directory / "ca.pem"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    path.chmod(0o600)
    return path.resolve()


def test_explicit_run_ca_is_used_with_verification_and_without_ambient_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_file = _run_ca(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", "/not-an-approved-ca")
    contexts: list[ssl.SSLContext] = []

    def capture_handlers(*handlers: object) -> object:
        https = next(handler for handler in handlers if isinstance(handler, HTTPSHandler))
        contexts.append(https._context)
        return object()

    monkeypatch.setattr(http_module, "build_opener", capture_handlers)
    ControllerHttpClient(_manifest("https://10.0.1.20"), ca_file=ca_file)

    assert contexts[0].check_hostname is True
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].cert_store_stats()["x509_ca"] == 1


@pytest.mark.parametrize("invalid", ["symlink", "hardlink", "mode", "parent", "body", "key"])
def test_run_ca_rejects_unsafe_file_or_invalid_certificate(
    tmp_path: Path,
    invalid: str,
) -> None:
    ca_file = _run_ca(tmp_path)
    if invalid == "symlink":
        alias = ca_file.with_name("alias.pem")
        alias.symlink_to(ca_file)
        ca_file = alias
    elif invalid == "hardlink":
        ca_file.with_name("alias.pem").hardlink_to(ca_file)
    elif invalid == "mode":
        ca_file.chmod(0o644)
    elif invalid == "parent":
        ca_file.parent.chmod(0o755)
    elif invalid == "body":
        ca_file.write_text("invalid certificate")
    else:
        ca_file.write_text(ca_file.read_text() + "\n-----BEGIN PRIVATE KEY-----\n")

    with pytest.raises(ControllerHttpError, match="controller_http_ca_file_invalid"):
        http_module._build_secure_opener(CookieJar(), ca_file)


def test_publish_results_uses_explicit_operator_mutation_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_body = (
        OperatorMutationRequest(
            expected_version=6,
            idempotency_key=IDEMPOTENCY_KEY,
        )
        .model_dump_json()
        .encode("utf-8")
    )

    def respond(request: Request, _: float, cookie_jar: CookieJar) -> StubResponse:
        if request.full_url.endswith("/auth/login"):
            return _login_response(cookie_jar)
        assert request.full_url == f"{ORIGIN}/api/v1/operator/rounds/{ROUND_ID}/publish-results"
        assert request.data == request_body
        return _json_response(
            {**_mutation_payload("publish_round_results"), "round_state": "Reveal"}
        )

    client, _ = _client(monkeypatch, respond)
    client.login("load-operator", SecretStr(PASSWORD_SENTINEL))
    response = client.post_mutation("publish_round_results", ROUND_ID, request_body)
    assert response.operation == "publish_round_results"
    assert response.round_state.value == "Reveal"
