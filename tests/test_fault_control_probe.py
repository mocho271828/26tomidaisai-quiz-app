from __future__ import annotations

import hashlib
import json
from email.message import Message
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request

import pytest

from quiz_app.aws_demo_paths import AwsDemoDeploymentValidationError
from quiz_app.load_test import fault_control_probe as probe_module
from quiz_app.load_test.fault_control_probe import (
    CLIENT_USER_AGENT,
    CONTROL_TIMEOUT_SECONDS,
    MAXIMUM_RESPONSE_BYTES,
    TOXIPROXY_PROXY_URL,
    FaultControlProbeError,
    FaultControlTransition,
    ToxiproxyControlClient,
    ToxiproxyProxyState,
)
from quiz_app.load_test.fault_topology import (
    TOXIPROXY_CONFIG_SHA256_ENV,
    TOXIPROXY_DATA_ADDRESS,
    TOXIPROXY_EXPECTED_LISTEN_ENV,
    TOXIPROXY_EXPECTED_NAME_ENV,
    TOXIPROXY_EXPECTED_UPSTREAM_ENV,
    TOXIPROXY_PROXY_NAME,
    RuntimeToxiproxyProxySnapshot,
    ToxiproxyRedisProxy,
    load_runtime_toxiproxy_proxy,
)

REDIS_PORT = 6380
LISTEN = f"{TOXIPROXY_DATA_ADDRESS}:{REDIS_PORT}"
UPSTREAM = f"10.0.2.15:{REDIS_PORT}"
SECRET_SENTINEL = "DO-NOT-LEAK-TOXIPROXY-RESPONSE"  # noqa: S105 -- 漏えい検査用


class StubResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        final_url: str = TOXIPROXY_PROXY_URL,
        content_type: str | None = "application/json",
        content_length: str | None = None,
        include_content_length: bool = True,
        content_encoding: str | None = None,
        duplicate_content_type: bool = False,
    ) -> None:
        self._body = body
        self._offset = 0
        self._status = status
        self._final_url = final_url
        self.headers = Message()
        if content_type is not None:
            self.headers.add_header("Content-Type", content_type)
            if duplicate_content_type:
                self.headers.add_header("Content-Type", content_type)
        if include_content_length:
            self.headers.add_header(
                "Content-Length",
                str(len(body)) if content_length is None else content_length,
            )
        if content_encoding is not None:
            self.headers.add_header("Content-Encoding", content_encoding)
        self.is_closed = False

    def getcode(self) -> int:
        return self._status

    def geturl(self) -> str:
        return self._final_url

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            result = self._body[self._offset :]
            self._offset = len(self._body)
            return result
        result = self._body[self._offset : self._offset + amount]
        self._offset += len(result)
        return result

    def close(self) -> None:
        self.is_closed = True


class FakeOpener:
    def __init__(self, outcomes: list[StubResponse | URLError]) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Request, float]] = []

    def open(self, request: Request, *, timeout: float) -> StubResponse:
        self.requests.append((request, timeout))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, URLError):
            raise outcome
        return outcome


def _expected_proxy() -> ToxiproxyRedisProxy:
    return ToxiproxyRedisProxy(
        name=TOXIPROXY_PROXY_NAME,
        listen=LISTEN,
        upstream=UPSTREAM,
        enabled=True,
    )


def _state_payload(enabled: object, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": TOXIPROXY_PROXY_NAME,
        "listen": LISTEN,
        "upstream": UPSTREAM,
        "enabled": enabled,
        "toxics": [],
    }
    payload.update(overrides)
    return payload


def _response(enabled: object, **kwargs: object) -> StubResponse:
    body = json.dumps(
        _state_payload(enabled),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return StubResponse(body, **kwargs)


def _state(enabled: bool) -> ToxiproxyProxyState:
    return ToxiproxyProxyState(
        name=TOXIPROXY_PROXY_NAME,
        listen=LISTEN,
        upstream=UPSTREAM,
        enabled=enabled,
        toxics=(),
    )


def _assert_error(code: str, operation: object) -> None:
    assert isinstance(operation, FaultControlProbeError)
    assert operation.code == code
    assert str(operation) == code
    assert SECRET_SENTINEL not in str(operation)


def test_secure_opener_disables_ambient_proxy_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: tuple[object, ...] = ()

    def capture_handlers(*handlers: object) -> object:
        nonlocal captured_handlers
        captured_handlers = handlers
        return object()

    monkeypatch.setattr(probe_module, "build_opener", capture_handlers)

    probe_module._build_secure_opener()

    proxy_handler = next(
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    )
    assert proxy_handler.proxies == {}
    redirect_handler = next(
        handler
        for handler in captured_handlers
        if isinstance(handler, probe_module._RejectRedirects)
    )
    with pytest.raises(FaultControlProbeError) as captured:
        redirect_handler.redirect_request(
            Request(TOXIPROXY_PROXY_URL),  # noqa: S310 -- 固定HTTP URLだけを検査する
            None,
            307,
            "redirect",
            Message(),
            "http://127.0.0.1:8474/proxies/other",
        )
    _assert_error("fault_control_redirect_refused", captured.value)


def test_probe_uses_only_fixed_get_and_validates_exact_response() -> None:
    response = _response(True)
    opener = FakeOpener([response])

    state = ToxiproxyControlClient(_expected_proxy(), opener=opener).probe()

    assert state.enabled is True
    assert state.toxics == ()
    assert response.is_closed is True
    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert request.full_url == TOXIPROXY_PROXY_URL
    assert request.method == "GET"
    assert request.data is None
    assert timeout == CONTROL_TIMEOUT_SECONDS
    assert headers == {
        "accept": "application/json",
        "accept-encoding": "identity",
        "user-agent": CLIENT_USER_AGENT,
    }


@pytest.mark.parametrize(
    ("enabled", "body"),
    [
        (False, b'{"enabled":false}'),
        (True, b'{"enabled":true}'),
    ],
)
def test_set_enabled_always_uses_get_patch_get_with_exact_body(
    enabled: bool,
    body: bytes,
) -> None:
    opener = FakeOpener([_response(not enabled), _response(enabled), _response(enabled)])

    transition = ToxiproxyControlClient(
        _expected_proxy(),
        opener=opener,
    ).set_enabled(enabled)

    assert transition.before.enabled is not enabled
    assert transition.after.enabled is enabled
    assert transition.reconciled is False
    assert [request.method for request, _ in opener.requests] == ["GET", "PATCH", "GET"]
    patch_request = opener.requests[1][0]
    patch_headers = {name.lower(): value for name, value in patch_request.header_items()}
    assert patch_request.full_url == TOXIPROXY_PROXY_URL
    assert patch_request.data == body
    assert patch_headers["content-type"] == "application/json"


def test_same_state_patch_is_repeated_for_idempotent_retry() -> None:
    opener = FakeOpener([_response(False), _response(False), _response(False)])

    transition = ToxiproxyControlClient(
        _expected_proxy(),
        opener=opener,
    ).set_enabled(False)

    assert transition.before.enabled is False
    assert transition.after.enabled is False
    assert opener.requests[1][0].data == b'{"enabled":false}'


def test_uncertain_patch_is_reconciled_by_final_get() -> None:
    opener = FakeOpener(
        [
            _response(True),
            URLError(SECRET_SENTINEL),
            _response(False),
        ]
    )

    transition = ToxiproxyControlClient(
        _expected_proxy(),
        opener=opener,
    ).set_enabled(False)

    assert transition.after.enabled is False
    assert transition.reconciled is True
    assert [request.method for request, _ in opener.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize("status", [500, 503])
def test_uncertain_server_status_is_reconciled_by_final_get(status: int) -> None:
    opener = FakeOpener([_response(True), _response(False, status=status), _response(False)])

    transition = ToxiproxyControlClient(
        _expected_proxy(),
        opener=opener,
    ).set_enabled(False)

    assert transition.after.enabled is False
    assert transition.reconciled is True


@pytest.mark.parametrize("status", [500, 503])
def test_uncertain_server_status_reconciles_before_response_body_validation(
    status: int,
) -> None:
    invalid_response = StubResponse(
        b"x" * (MAXIMUM_RESPONSE_BYTES + 1),
        status=status,
        content_type="text/plain",
        content_length="not-a-length",
        content_encoding="gzip",
    )
    opener = FakeOpener([_response(True), invalid_response, _response(False)])

    transition = ToxiproxyControlClient(
        _expected_proxy(),
        opener=opener,
    ).set_enabled(False)

    assert transition.after.enabled is False
    assert transition.reconciled is True
    assert invalid_response.is_closed is True


def test_uncertain_patch_fails_when_final_get_does_not_confirm_target() -> None:
    opener = FakeOpener(
        [
            _response(True),
            URLError(SECRET_SENTINEL),
            _response(True),
        ]
    )

    with pytest.raises(FaultControlProbeError) as captured:
        ToxiproxyControlClient(_expected_proxy(), opener=opener).set_enabled(False)

    _assert_error("fault_control_result_unknown", captured.value)


def test_patch_protocol_rejection_still_runs_fixed_final_get() -> None:
    invalid_patch_response = StubResponse(
        json.dumps(_state_payload(False, extra=SECRET_SENTINEL)).encode()
    )
    opener = FakeOpener([_response(True), invalid_patch_response, _response(False)])

    with pytest.raises(FaultControlProbeError) as captured:
        ToxiproxyControlClient(_expected_proxy(), opener=opener).set_enabled(False)

    _assert_error("fault_control_response_schema_invalid", captured.value)
    assert [request.method for request, _ in opener.requests] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            StubResponse(
                b'{"name":"gs-app-tomidaisai-aws-demo-redis",'
                b'"listen":"172.30.250.2:6380","upstream":"10.0.2.15:6380",'
                b'"enabled":true,"enabled":false,"toxics":[]}'
            ),
            "fault_control_response_json_invalid",
        ),
        (
            StubResponse(json.dumps(_state_payload(True, extra=SECRET_SENTINEL)).encode()),
            "fault_control_response_schema_invalid",
        ),
        (
            _response(True, content_type="text/plain"),
            "fault_control_response_content_type_invalid",
        ),
        (
            _response(True, duplicate_content_type=True),
            "fault_control_response_content_type_invalid",
        ),
        (_response(True, status=201), "fault_control_response_status_invalid"),
        (
            StubResponse(b"x" * (MAXIMUM_RESPONSE_BYTES + 1)),
            "fault_control_response_too_large",
        ),
        (
            StubResponse(b"{}", content_length="3"),
            "fault_control_response_length_invalid",
        ),
        (
            StubResponse(
                json.dumps(_state_payload(True, toxics=[{"name": SECRET_SENTINEL}])).encode()
            ),
            "fault_control_response_schema_invalid",
        ),
        (_response("true"), "fault_control_response_schema_invalid"),
        (
            StubResponse(
                json.dumps(_state_payload(True, upstream=f"{SECRET_SENTINEL}:6380")).encode()
            ),
            "fault_control_response_identity_mismatch",
        ),
    ],
)
def test_probe_rejects_non_exact_or_unsafe_response(
    response: StubResponse,
    code: str,
) -> None:
    with pytest.raises(FaultControlProbeError) as captured:
        ToxiproxyControlClient(
            _expected_proxy(),
            opener=FakeOpener([response]),
        ).probe()

    _assert_error(code, captured.value)
    assert response.is_closed is True


def test_probe_rejects_changed_final_url() -> None:
    response = _response(True, final_url="http://127.0.0.1:8474/proxies/other")

    with pytest.raises(FaultControlProbeError) as captured:
        ToxiproxyControlClient(
            _expected_proxy(),
            opener=FakeOpener([response]),
        ).probe()

    _assert_error("fault_control_redirect_refused", captured.value)


def test_secret_free_runtime_loader_revalidates_external_config_and_digest(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "toxiproxy.json"
    body = (
        json.dumps(
            [
                {
                    "name": TOXIPROXY_PROXY_NAME,
                    "listen": LISTEN,
                    "upstream": UPSTREAM,
                    "enabled": True,
                }
            ],
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    config_path.write_bytes(body)
    config_path.chmod(0o600)

    proxy = load_runtime_toxiproxy_proxy(
        hashlib.sha256(body).hexdigest(),
        config_path,
    )

    assert proxy == _expected_proxy()


def test_in_memory_snapshot_loader_requires_all_exact_topology_fields() -> None:
    environment = {
        TOXIPROXY_CONFIG_SHA256_ENV: "a" * 64,
        TOXIPROXY_EXPECTED_NAME_ENV: TOXIPROXY_PROXY_NAME,
        TOXIPROXY_EXPECTED_LISTEN_ENV: LISTEN,
        TOXIPROXY_EXPECTED_UPSTREAM_ENV: UPSTREAM,
    }

    assert probe_module._load_expected_proxy(environment) == _expected_proxy()
    environment.pop(TOXIPROXY_EXPECTED_UPSTREAM_ENV)
    with pytest.raises(AwsDemoDeploymentValidationError):
        probe_module._load_expected_proxy(environment)


@pytest.mark.parametrize(
    ("operation", "enabled", "reconciled"),
    [
        ("probe", True, False),
        ("disable", False, False),
        ("enable", True, True),
    ],
)
def test_cli_emits_one_canonical_json_line_with_one_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    enabled: bool,
    reconciled: bool,
) -> None:
    expected_proxy = _expected_proxy()

    class StubClient:
        def __init__(self, proxy: ToxiproxyRedisProxy) -> None:
            assert proxy == expected_proxy

        def probe(self) -> ToxiproxyProxyState:
            return _state(enabled)

        def set_enabled(self, requested: bool) -> FaultControlTransition:
            assert requested is enabled
            return FaultControlTransition(
                before=_state(not enabled),
                after=_state(enabled),
                reconciled=reconciled,
            )

    runtime_snapshot = RuntimeToxiproxyProxySnapshot(
        configuration_sha256="a" * 64,
        proxy=expected_proxy,
    )
    monkeypatch.setenv(TOXIPROXY_CONFIG_SHA256_ENV, "a" * 64)
    monkeypatch.setenv(TOXIPROXY_EXPECTED_NAME_ENV, TOXIPROXY_PROXY_NAME)
    monkeypatch.setenv(TOXIPROXY_EXPECTED_LISTEN_ENV, LISTEN)
    monkeypatch.setenv(TOXIPROXY_EXPECTED_UPSTREAM_ENV, UPSTREAM)
    monkeypatch.setattr(
        probe_module,
        "load_runtime_toxiproxy_proxy_snapshot",
        lambda _environment: runtime_snapshot,
    )
    monkeypatch.setattr(
        probe_module,
        "load_runtime_toxiproxy_proxy",
        lambda _digest: pytest.fail("formal client must not reread config file"),
    )
    monkeypatch.setattr(probe_module, "ToxiproxyControlClient", StubClient)

    assert probe_module.main([operation]) == 0

    captured = capsys.readouterr()
    expected = {
        "enabled": enabled,
        "format": "toxiproxy-control-result-v1",
        "name": TOXIPROXY_PROXY_NAME,
        "operation": operation,
        "reconciled": reconciled,
    }
    assert captured.out == json.dumps(expected, separators=(",", ":"), sort_keys=True) + "\n"
    assert captured.err == ""


def test_cli_maps_configuration_failure_to_fixed_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AWS_DEMO_TOXIPROXY_CONFIG_SHA256", "a" * 64)

    def reject(_: str) -> ToxiproxyRedisProxy:
        raise AwsDemoDeploymentValidationError(SECRET_SENTINEL)

    monkeypatch.setattr(probe_module, "load_runtime_toxiproxy_proxy", reject)

    assert probe_module.main(["probe"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "fault_control_configuration_invalid\n"
    assert SECRET_SENTINEL not in captured.err
