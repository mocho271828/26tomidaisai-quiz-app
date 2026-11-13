"""固定Toxiproxy control APIをfail-closedで照合・操作する短命client。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Final, Literal, NoReturn, Protocol, TypeAlias, cast
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

from quiz_app.aws_demo_paths import AwsDemoDeploymentValidationError
from quiz_app.load_test.fault_topology import (
    TOXIPROXY_CONFIG_SHA256_ENV,
    TOXIPROXY_EXPECTED_LISTEN_ENV,
    TOXIPROXY_EXPECTED_NAME_ENV,
    TOXIPROXY_EXPECTED_UPSTREAM_ENV,
    TOXIPROXY_PROXY_NAME,
    ToxiproxyRedisProxy,
    load_runtime_toxiproxy_proxy,
    load_runtime_toxiproxy_proxy_snapshot,
)

TOXIPROXY_CONTROL_ORIGIN: Final = "http://172.30.251.2:8474"
TOXIPROXY_PROXY_PATH: Final = f"/proxies/{TOXIPROXY_PROXY_NAME}"
TOXIPROXY_PROXY_URL: Final = f"{TOXIPROXY_CONTROL_ORIGIN}{TOXIPROXY_PROXY_PATH}"
JSON_CONTENT_TYPE: Final = "application/json"
CONTROL_TIMEOUT_SECONDS: Final = 5.0
MAXIMUM_RESPONSE_BYTES: Final = 4096
CLIENT_USER_AGENT: Final = "gs-app-tomidaisai-fault-control/1"
CLI_RESULT_FORMAT: Final = "toxiproxy-control-result-v1"

FaultControlOperation: TypeAlias = Literal["probe", "disable", "enable"]
FaultControlErrorCode: TypeAlias = Literal[
    "fault_control_configuration_invalid",
    "fault_control_redirect_refused",
    "fault_control_transport_failed",
    "fault_control_response_encoding_invalid",
    "fault_control_response_length_invalid",
    "fault_control_response_too_large",
    "fault_control_response_body_invalid",
    "fault_control_response_read_failed",
    "fault_control_response_status_invalid",
    "fault_control_control_unavailable",
    "fault_control_response_content_type_invalid",
    "fault_control_response_json_invalid",
    "fault_control_response_schema_invalid",
    "fault_control_response_identity_mismatch",
    "fault_control_response_state_mismatch",
    "fault_control_result_unknown",
    "fault_control_request_invalid",
]

_PATCH_BODIES: Final = {
    False: b'{"enabled":false}',
    True: b'{"enabled":true}',
}


class FaultControlProbeError(RuntimeError):
    """入力値や応答本文を含まない固定codeだけを返す。"""

    def __init__(self, code: FaultControlErrorCode) -> None:
        self.code = code
        super().__init__(code)


class _OutcomeUncertainError(FaultControlProbeError):
    """PATCHが反映された可能性をGETで再照合すべき失敗。"""


class _DuplicateJsonKeyError(ValueError):
    """曖昧なJSON objectを応答値なしで拒否する内部signal。"""


class _Response(Protocol):
    headers: Message

    def getcode(self) -> int: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _Opener(Protocol):
    def open(self, request: Request, *, timeout: float) -> _Response: ...


class ToxiproxyProxyState(BaseModel):
    """control APIから許可するexact proxy response。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: StrictStr
    listen: StrictStr
    upstream: StrictStr
    enabled: StrictBool
    toxics: tuple[()]


class FaultControlCliResult(BaseModel):
    """host executorが唯一受理するone-line stdout schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: StrictBool
    format: Literal["toxiproxy-control-result-v1"] = CLI_RESULT_FORMAT
    name: Literal["gs-app-tomidaisai-aws-demo-redis"] = TOXIPROXY_PROXY_NAME
    operation: FaultControlOperation
    reconciled: StrictBool


@dataclass(frozen=True, slots=True)
class FaultControlTransition:
    """PATCH前後の検証済みstateと不確定応答の照合有無。"""

    before: ToxiproxyProxyState
    after: ToxiproxyProxyState
    reconciled: bool


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    status_code: int
    headers: Message
    body: bytes


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> Request | None:
        raise FaultControlProbeError("fault_control_redirect_refused")


class _FixedErrorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "fault_control_cli_invalid\n")


def _build_secure_opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}), _RejectRedirects())


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_non_json_number(_: str) -> None:
    raise ValueError


def _content_type(headers: Message) -> str:
    values = headers.get_all("Content-Type", [])
    if len(values) != 1:
        raise FaultControlProbeError("fault_control_response_content_type_invalid")
    return values[0].partition(";")[0].strip().lower()


def _content_length(headers: Message) -> int | None:
    values = headers.get_all("Content-Length", [])
    if not values:
        return None
    if len(values) != 1:
        raise FaultControlProbeError("fault_control_response_length_invalid")
    value_text = values[0]
    if not value_text.isascii() or not value_text.isdecimal():
        raise FaultControlProbeError("fault_control_response_length_invalid")
    try:
        value = int(value_text)
    except ValueError as error:
        raise FaultControlProbeError("fault_control_response_length_invalid") from error
    if value_text != str(value):
        raise FaultControlProbeError("fault_control_response_length_invalid")
    if value > MAXIMUM_RESPONSE_BYTES:
        raise FaultControlProbeError("fault_control_response_too_large")
    return value


def _read_bounded(response: _Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAXIMUM_RESPONSE_BYTES:
        chunk = response.read(min(1024, MAXIMUM_RESPONSE_BYTES + 1 - total))
        if not isinstance(chunk, bytes):
            raise FaultControlProbeError("fault_control_response_body_invalid")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAXIMUM_RESPONSE_BYTES:
        raise FaultControlProbeError("fault_control_response_too_large")
    return b"".join(chunks)


def _decode_state(body: bytes) -> ToxiproxyProxyState:
    try:
        json.loads(
            body,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_json_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        ValueError,
    ) as error:
        raise FaultControlProbeError("fault_control_response_json_invalid") from error
    try:
        return ToxiproxyProxyState.model_validate_json(body, strict=True)
    except ValidationError as error:
        raise FaultControlProbeError("fault_control_response_schema_invalid") from error


class ToxiproxyControlClient:
    """固定origin・固定proxyだけを操作するtyped low-level client。"""

    def __init__(
        self,
        expected_proxy: ToxiproxyRedisProxy,
        *,
        opener: _Opener | None = None,
    ) -> None:
        if expected_proxy.name != TOXIPROXY_PROXY_NAME:
            raise FaultControlProbeError("fault_control_request_invalid")
        self._expected_proxy = expected_proxy
        self._opener = opener if opener is not None else _build_secure_opener()

    def probe(self) -> ToxiproxyProxyState:
        """固定proxyをGETし、identity・空toxicsを含む全契約を検証する。"""

        return self._request_state("GET", None)

    def set_enabled(self, enabled: bool) -> FaultControlTransition:
        """GET→PATCH→GETを行い、不確定PATCHだけ最終GETで照合する。"""

        if not isinstance(enabled, bool):
            raise FaultControlProbeError("fault_control_request_invalid")
        before = self.probe()
        try:
            patched = self._request_state("PATCH", _PATCH_BODIES[enabled])
        except _OutcomeUncertainError as error:
            return self._reconcile_uncertain(before, enabled, error)
        except FaultControlProbeError:
            # PATCHのprotocol違反を成功扱いにはしないが、副作用後でも必ず固定GETで状態を照合する。
            try:
                self.probe()
            except FaultControlProbeError:
                pass
            raise
        try:
            after = self.probe()
        except _OutcomeUncertainError as error:
            raise FaultControlProbeError("fault_control_result_unknown") from error
        if patched.enabled is not enabled or after.enabled is not enabled:
            raise FaultControlProbeError("fault_control_response_state_mismatch")
        return FaultControlTransition(before=before, after=after, reconciled=False)

    def _reconcile_uncertain(
        self,
        before: ToxiproxyProxyState,
        enabled: bool,
        original_error: _OutcomeUncertainError,
    ) -> FaultControlTransition:
        try:
            after = self.probe()
        except FaultControlProbeError as error:
            raise FaultControlProbeError("fault_control_result_unknown") from error
        if after.enabled is not enabled:
            raise FaultControlProbeError("fault_control_result_unknown") from original_error
        return FaultControlTransition(before=before, after=after, reconciled=True)

    def _request_state(
        self,
        method: Literal["GET", "PATCH"],
        body: bytes | None,
    ) -> ToxiproxyProxyState:
        exchange = self._exchange(method, body)
        if exchange.status_code in {500, 503}:
            raise _OutcomeUncertainError("fault_control_control_unavailable")
        if exchange.status_code != 200:
            raise FaultControlProbeError("fault_control_response_status_invalid")
        if _content_type(exchange.headers) != JSON_CONTENT_TYPE:
            raise FaultControlProbeError("fault_control_response_content_type_invalid")
        state = _decode_state(exchange.body)
        if (
            state.name != self._expected_proxy.name
            or state.listen != self._expected_proxy.listen
            or state.upstream != self._expected_proxy.upstream
        ):
            raise FaultControlProbeError("fault_control_response_identity_mismatch")
        return state

    def _exchange(
        self,
        method: Literal["GET", "PATCH"],
        body: bytes | None,
    ) -> _ExchangeResult:
        headers = {
            "Accept": JSON_CONTENT_TYPE,
            "Accept-Encoding": "identity",
            "User-Agent": CLIENT_USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = JSON_CONTENT_TYPE
        try:
            request = Request(  # noqa: S310 -- 固定internal control URLだけを使用する。
                TOXIPROXY_PROXY_URL,
                data=body,
                headers=headers,
                method=method,
            )
            try:
                response: _Response = self._opener.open(
                    request,
                    timeout=CONTROL_TIMEOUT_SECONDS,
                )
            except HTTPError as error:
                response = error
        except FaultControlProbeError:
            raise
        except (OSError, TimeoutError, URLError, ValueError) as error:
            raise _OutcomeUncertainError("fault_control_transport_failed") from error
        outcome_uncertain = False
        try:
            if response.geturl() != TOXIPROXY_PROXY_URL:
                raise FaultControlProbeError("fault_control_redirect_refused")
            status_code = response.getcode()
            if type(status_code) is not int:
                raise FaultControlProbeError("fault_control_response_status_invalid")
            # PATCH適用後の500/503本文は信頼せず、状態GETによる照合へ必ず進める。
            if status_code in {500, 503}:
                outcome_uncertain = True
                raise _OutcomeUncertainError("fault_control_control_unavailable")
            encoding_values = response.headers.get_all("Content-Encoding", [])
            if len(encoding_values) > 1 or (
                encoding_values and encoding_values[0].strip().lower() not in {"", "identity"}
            ):
                raise FaultControlProbeError("fault_control_response_encoding_invalid")
            expected_length = _content_length(response.headers)
            response_body = _read_bounded(response)
            if expected_length is not None and expected_length != len(response_body):
                raise FaultControlProbeError("fault_control_response_length_invalid")
            return _ExchangeResult(
                status_code=status_code,
                headers=response.headers,
                body=response_body,
            )
        except FaultControlProbeError:
            raise
        except (OSError, ValueError) as error:
            outcome_uncertain = True
            raise _OutcomeUncertainError("fault_control_transport_failed") from error
        finally:
            try:
                response.close()
            except (OSError, ValueError) as error:
                if not outcome_uncertain:
                    raise FaultControlProbeError("fault_control_response_read_failed") from error


def _build_parser() -> _FixedErrorArgumentParser:
    parser = _FixedErrorArgumentParser(description="固定Toxiproxy proxyの状態照合と切替")
    parser.add_argument("operation", choices=("probe", "disable", "enable"))
    return parser


def _emit_success(
    operation: FaultControlOperation,
    state: ToxiproxyProxyState,
    *,
    reconciled: bool,
) -> None:
    result = FaultControlCliResult(
        operation=operation,
        enabled=state.enabled,
        reconciled=reconciled,
    )
    payload = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    sys.stdout.write(f"{payload}\n")


def _load_expected_proxy(environment: Mapping[str, str]) -> ToxiproxyRedisProxy:
    """formal snapshotまたはdiagnostic用RO configから期待proxyを得る。"""

    snapshot_fields = (
        TOXIPROXY_EXPECTED_NAME_ENV,
        TOXIPROXY_EXPECTED_LISTEN_ENV,
        TOXIPROXY_EXPECTED_UPSTREAM_ENV,
    )
    present = tuple(field in environment for field in snapshot_fields)
    if any(present):
        if not all(present) or TOXIPROXY_CONFIG_SHA256_ENV not in environment:
            raise AwsDemoDeploymentValidationError("toxiproxy_snapshot_invalid")
        return load_runtime_toxiproxy_proxy_snapshot(environment).proxy
    expected_sha256 = environment.get(TOXIPROXY_CONFIG_SHA256_ENV)
    if expected_sha256 is None:
        raise AwsDemoDeploymentValidationError("toxiproxy_snapshot_invalid")
    return load_runtime_toxiproxy_proxy(expected_sha256)


def main(argv: list[str] | None = None) -> int:
    namespace = _build_parser().parse_args(argv)
    operation = cast(FaultControlOperation, namespace.operation)
    try:
        client = ToxiproxyControlClient(_load_expected_proxy(os.environ))
        if operation == "probe":
            _emit_success(operation, client.probe(), reconciled=False)
        else:
            transition = client.set_enabled(operation == "enable")
            _emit_success(
                operation,
                transition.after,
                reconciled=transition.reconciled,
            )
    except AwsDemoDeploymentValidationError:
        sys.stderr.write("fault_control_configuration_invalid\n")
        return 1
    except FaultControlProbeError as error:
        sys.stderr.write(f"{error.code}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
