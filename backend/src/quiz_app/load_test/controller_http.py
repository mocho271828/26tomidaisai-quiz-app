from __future__ import annotations

import json
import os
import ssl
import stat
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from http.cookiejar import Cookie, CookieJar
from http.cookies import CookieError, Morsel, SimpleCookie
from pathlib import Path
from typing import Generic, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID

from pydantic import BaseModel, RootModel, SecretStr, ValidationError

from quiz_app.audience.contracts import TimeResponse
from quiz_app.contracts import ApiErrorResponse, LiveHealthResponse, ReadinessHealthResponse
from quiz_app.load_test.controller_contracts import (
    ControllerApiMutationError,
    ControllerOperation,
)
from quiz_app.load_test.legacy_control_contracts import (
    OperatorStateResponse,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.operator.contracts import (
    OperatorIdentityResponse,
    OperatorLoginRequest,
    OperatorSessionResponse,
)
from quiz_app.operator.control_contracts import (
    NextRoundMutationRequest,
    OperatorLatestRankingResponse,
    OperatorMutationRequest,
    OperatorMutationResponse,
    ScheduleRoundMutationRequest,
)

MAXIMUM_RESPONSE_BYTES = 1_000_000
MAXIMUM_MUTATION_REQUEST_BYTES = 10_000
READ_TIMEOUT_SECONDS = 5.0
MUTATION_TIMEOUT_SECONDS = 30.0
OPERATOR_SESSION_COOKIE = "quiz_operator_session"
OPERATOR_CSRF_COOKIE = "quiz_operator_csrf"
JSON_CONTENT_TYPE = "application/json"

_ERROR_CODES_BY_STATUS = {
    401: frozenset({"operator_authentication_required"}),
    403: frozenset({"operator_request_forbidden"}),
    404: frozenset({"demo_auth_not_available", "resource_not_found"}),
    409: frozenset({"idempotency_key_conflict", "state_conflict"}),
    422: frozenset({"request_validation_failed"}),
    500: frozenset({"domain_error", "operator_control_error"}),
    503: frozenset(
        {
            "database_temporarily_unavailable",
            "domain_temporarily_unavailable",
            "round_backup_failed",
        }
    ),
}

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
ReauthenticationCallback = Callable[["ControllerHttpClient"], None]


class ControllerHttpError(RuntimeError):
    """秘密値を含めず、固定されたtransport拒否codeだけを表す。"""


class ControllerHttpResponseError(ControllerHttpError):
    """検証済みのHTTP statusと固定error codeを呼出側へ返す。"""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(f"controller_http_response:{status_code}:{code}")


class _Response(Protocol):
    headers: Message

    def getcode(self) -> int: ...

    def geturl(self) -> str: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _Opener(Protocol):
    def open(self, request: Request, *, timeout: float) -> _Response: ...


class _DuplicateJsonKeyError(ValueError):
    pass


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
        raise ControllerHttpError("controller_http_redirect_refused")


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    status_code: int
    headers: Message
    body: bytes


def _read_ca_bundle(path: Path) -> str:
    """run専用CAを差替えのないfileからboundedに読み、秘密鍵の混入を拒否する。"""

    descriptor = -1
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ControllerHttpError("controller_http_ca_file_invalid")
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ControllerHttpError("controller_http_ca_file_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= 1_048_576
            ):
                raise ControllerHttpError("controller_http_ca_file_invalid")
            body = source.read(1_048_577)
            after = os.fstat(source.fileno())
            if (
                after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
                or len(body) != before.st_size
            ):
                raise ControllerHttpError("controller_http_ca_file_invalid")
        if b"PRIVATE KEY" in body:
            raise ControllerHttpError("controller_http_ca_file_invalid")
        return body.decode("ascii")
    except (OSError, UnicodeDecodeError):
        raise ControllerHttpError("controller_http_ca_file_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _build_secure_opener(cookie_jar: CookieJar, ca_file: Path | None = None) -> OpenerDirector:
    try:
        context = (
            ssl.create_default_context(cadata=_read_ca_bundle(ca_file))
            if ca_file is not None
            else ssl.create_default_context()
        )
    except ssl.SSLError:
        raise ControllerHttpError("controller_http_ca_file_invalid") from None
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ControllerHttpError("controller_http_tls_configuration_invalid")
    return build_opener(
        ProxyHandler({}),
        HTTPCookieProcessor(cookie_jar),
        _RejectRedirects(),
        HTTPSHandler(context=context),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_non_json_number(_: str) -> None:
    raise ValueError


def _validate_json_shape(body: bytes) -> None:
    try:
        json.loads(
            body,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError) as error:
        raise ControllerHttpError("controller_http_response_json_invalid") from error


def _decode_model(body: bytes, model: type[ResponseModel]) -> ResponseModel:
    _validate_json_shape(body)
    try:
        return model.model_validate_json(body, strict=True)
    except ValidationError as error:
        raise ControllerHttpError("controller_http_response_schema_invalid") from error


def _content_type(headers: Message) -> str:
    values = headers.get_all("Content-Type", [])
    if len(values) != 1:
        raise ControllerHttpError("controller_http_response_content_type_invalid")
    return values[0].partition(";")[0].strip().lower()


def _content_length(headers: Message) -> int | None:
    values = headers.get_all("Content-Length", [])
    if not values:
        return None
    if len(values) != 1:
        raise ControllerHttpError("controller_http_response_length_invalid")
    try:
        value = int(values[0])
    except ValueError as error:
        raise ControllerHttpError("controller_http_response_length_invalid") from error
    if value < 0 or value > MAXIMUM_RESPONSE_BYTES:
        raise ControllerHttpError("controller_http_response_too_large")
    return value


def _cookie_from_jar(cookie_jar: CookieJar, name: str, origin_host: str) -> Cookie:
    matches = [cookie for cookie in cookie_jar if cookie.name == name]
    if len(matches) != 1:
        raise ControllerHttpError("controller_http_cookie_contract_invalid")
    cookie = matches[0]
    if (
        not cookie.value
        or not cookie.secure
        or cookie.domain_specified
        or cookie.domain.casefold() != origin_host.casefold()
    ):
        raise ControllerHttpError("controller_http_cookie_contract_invalid")
    return cookie


def _validate_cookie_morsel(
    morsel: Morsel[str],
    *,
    path: str,
    is_http_only: bool,
) -> None:
    if (
        not morsel.value
        or morsel["path"] != path
        or not morsel["secure"]
        or bool(morsel["httponly"]) is not is_http_only
        or morsel["samesite"].casefold() != "strict"
        or morsel["domain"] != ""
    ):
        raise ControllerHttpError("controller_http_cookie_contract_invalid")


def _parse_login_cookie_headers(headers: Message) -> dict[str, Morsel[str]]:
    values = headers.get_all("Set-Cookie", [])
    if len(values) != 2:
        raise ControllerHttpError("controller_http_cookie_contract_invalid")
    parsed: dict[str, Morsel[str]] = {}
    for value in values:
        cookie = SimpleCookie()
        try:
            cookie.load(value)
        except CookieError as error:
            raise ControllerHttpError("controller_http_cookie_contract_invalid") from error
        if len(cookie) != 1:
            raise ControllerHttpError("controller_http_cookie_contract_invalid")
        name, morsel = next(iter(cookie.items()))
        if name in parsed:
            raise ControllerHttpError("controller_http_cookie_contract_invalid")
        parsed[name] = morsel
    if set(parsed) != {OPERATOR_SESSION_COOKIE, OPERATOR_CSRF_COOKIE}:
        raise ControllerHttpError("controller_http_cookie_contract_invalid")
    return parsed


class ControllerHttpClient:
    """承認済みmanifest originの固定APIだけを呼ぶ低level typed client。"""

    def __init__(
        self,
        manifest: LoadTestManifest,
        *,
        reauthenticate: ReauthenticationCallback | None = None,
        ca_file: Path | None = None,
    ) -> None:
        try:
            public_origin = LoadTestManifest.validate_public_origin(manifest.public_origin)
        except (ValidationError, ValueError) as error:
            raise ControllerHttpError("controller_http_origin_invalid") from error
        parsed_origin = urlsplit(public_origin)
        if parsed_origin.hostname is None:
            raise ControllerHttpError("controller_http_origin_invalid")
        if not isinstance(manifest.event.id, UUID):
            raise ControllerHttpError("controller_http_manifest_invalid")
        self._origin = public_origin
        self._origin_host = parsed_origin.hostname
        self._event_id = manifest.event.id
        self._cookie_jar = CookieJar()
        self._opener: _Opener = _build_secure_opener(self._cookie_jar, ca_file)
        self._reauthenticate = reauthenticate
        self._bound_operator: OperatorIdentityResponse | None = None

    def login(self, username: str, password: SecretStr) -> OperatorSessionResponse:
        """passwordを保持せず、認証応答とCookie属性を一度に検証する。"""

        try:
            request = OperatorLoginRequest(username=username, password=password)
            body = json.dumps(
                {
                    "username": request.username,
                    "password": request.password.get_secret_value(),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, ValidationError) as error:
            raise ControllerHttpError("controller_http_login_request_invalid") from error
        try:
            result = self._request_model(
                method="POST",
                path="/api/v1/operator/auth/login",
                response_model=OperatorSessionResponse,
                body=body,
                timeout=READ_TIMEOUT_SECONDS,
                allow_reauthentication=False,
            )
            self._validate_login_cookies(result.headers)
            if self._bound_operator is None:
                self._bound_operator = result.model.operator
            elif self._bound_operator != result.model.operator:
                raise ControllerHttpError("controller_http_operator_identity_drift")
            return result.model
        except ControllerHttpError:
            self._cookie_jar.clear()
            raise

    def authenticate(self, username: str, password: SecretStr) -> OperatorSessionResponse:
        return self.login(username, password)

    def get_session(self) -> OperatorSessionResponse:
        return self._request_model(
            method="GET",
            path="/api/v1/operator/auth/session",
            response_model=OperatorSessionResponse,
            timeout=READ_TIMEOUT_SECONDS,
        ).model

    def get_server_time(self) -> TimeResponse:
        return self._request_model(
            method="GET",
            path="/api/v1/time",
            response_model=TimeResponse,
            timeout=READ_TIMEOUT_SECONDS,
        ).model

    def get_readiness(self) -> ReadinessHealthResponse:
        return self._request_model(
            method="GET",
            path="/api/health/ready",
            response_model=ReadinessHealthResponse,
            timeout=READ_TIMEOUT_SECONDS,
            successful_statuses=frozenset({200, 503}),
        ).model

    def get_live_health(self) -> LiveHealthResponse:
        return self._request_model(
            method="GET",
            path="/api/health/live",
            response_model=LiveHealthResponse,
            timeout=READ_TIMEOUT_SECONDS,
        ).model

    def get_liveness(self) -> LiveHealthResponse:
        return self.get_live_health()

    def get_event_state(self) -> OperatorStateResponse:
        result = self._request_model(
            method="GET",
            path="/api/v1/operator/state",
            response_model=OperatorStateResponse,
            timeout=READ_TIMEOUT_SECONDS,
        ).model
        if result.event_id != self._event_id:
            raise ControllerHttpError("controller_http_response_identity_mismatch")
        return result

    def get_latest_ranking(self) -> OperatorLatestRankingResponse | None:
        return self._request_model(
            method="GET",
            path="/api/v1/operator/ranking",
            response_model=RootModel[OperatorLatestRankingResponse | None],
            timeout=READ_TIMEOUT_SECONDS,
        ).model.root

    def post_mutation(
        self,
        operation: ControllerOperation,
        round_id: UUID,
        body: bytes,
    ) -> OperatorMutationResponse:
        path_by_operation = {
            "begin_preparation": "prepare",
            "schedule_round": "schedule",
            "publish_round_results": "publish-results",
            "advance_round": "advance",
            "complete_event": "complete-event",
        }
        path_suffix = path_by_operation.get(operation)
        if path_suffix is None:
            raise ControllerHttpError("controller_http_operation_invalid")
        if not isinstance(round_id, UUID):
            raise ControllerHttpError("controller_http_round_id_invalid")
        if not isinstance(body, bytes) or not body or len(body) > MAXIMUM_MUTATION_REQUEST_BYTES:
            raise ControllerHttpError("controller_http_mutation_request_invalid")
        request_model = {
            "advance_round": NextRoundMutationRequest,
            "schedule_round": ScheduleRoundMutationRequest,
        }.get(operation, OperatorMutationRequest)
        try:
            _validate_json_shape(body)
            request_model.model_validate_json(body, strict=True)
        except (ControllerHttpError, ValidationError) as error:
            raise ControllerHttpError("controller_http_mutation_request_invalid") from error
        try:
            return self._mutation_body(round_id, path_suffix, operation, body)
        except ControllerHttpResponseError as error:
            if error.status_code == 503 and error.code == "round_backup_failed":
                raise ControllerApiMutationError("round_backup_failed") from error
            raise

    def prepare_round(
        self,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        return self.post_mutation(
            "begin_preparation",
            round_id,
            request.model_dump_json().encode("utf-8"),
        )

    def schedule_round(
        self,
        round_id: UUID,
        request: ScheduleRoundMutationRequest,
    ) -> OperatorMutationResponse:
        return self.post_mutation(
            "schedule_round",
            round_id,
            request.model_dump_json().encode("utf-8"),
        )

    def advance_round(
        self,
        round_id: UUID,
        request: NextRoundMutationRequest,
    ) -> OperatorMutationResponse:
        return self.post_mutation(
            "advance_round",
            round_id,
            request.model_dump_json().encode("utf-8"),
        )

    def complete_event(
        self,
        round_id: UUID,
        request: OperatorMutationRequest,
    ) -> OperatorMutationResponse:
        return self.post_mutation(
            "complete_event",
            round_id,
            request.model_dump_json().encode("utf-8"),
        )

    def logout(self) -> None:
        self._request_empty(
            method="POST",
            path="/api/v1/operator/auth/logout",
            timeout=READ_TIMEOUT_SECONDS,
            requires_csrf=True,
        )
        self._cookie_jar.clear()
        self._bound_operator = None

    def _mutation_body(
        self,
        round_id: UUID,
        path_suffix: str,
        operation: ControllerOperation,
        body: bytes,
    ) -> OperatorMutationResponse:
        result = self._request_model(
            method="POST",
            path=f"/api/v1/operator/rounds/{round_id}/{path_suffix}",
            response_model=OperatorMutationResponse,
            body=body,
            timeout=MUTATION_TIMEOUT_SECONDS,
            requires_csrf=True,
        ).model
        if (
            result.operation != operation
            or result.event_id != self._event_id
            or result.round_version is None
            or result.round_state is None
            or (operation != "advance_round" and result.round_id != round_id)
            or (
                operation == "advance_round"
                and (result.round_id is None or result.round_id == round_id)
            )
        ):
            raise ControllerHttpError("controller_http_response_identity_mismatch")
        return result

    def _validate_login_cookies(self, headers: Message) -> None:
        parsed = _parse_login_cookie_headers(headers)
        _validate_cookie_morsel(
            parsed[OPERATOR_SESSION_COOKIE],
            path="/api/v1/operator",
            is_http_only=True,
        )
        _validate_cookie_morsel(
            parsed[OPERATOR_CSRF_COOKIE],
            path="/",
            is_http_only=False,
        )
        session = _cookie_from_jar(
            self._cookie_jar,
            OPERATOR_SESSION_COOKIE,
            self._origin_host,
        )
        csrf = _cookie_from_jar(
            self._cookie_jar,
            OPERATOR_CSRF_COOKIE,
            self._origin_host,
        )
        if (
            session.path != "/api/v1/operator"
            or csrf.path != "/"
            or session.value != parsed[OPERATOR_SESSION_COOKIE].value
            or csrf.value != parsed[OPERATOR_CSRF_COOKIE].value
            or len(list(self._cookie_jar)) != 2
        ):
            raise ControllerHttpError("controller_http_cookie_contract_invalid")

    def _csrf_token(self) -> str:
        return _cookie_from_jar(
            self._cookie_jar,
            OPERATOR_CSRF_COOKIE,
            self._origin_host,
        ).value

    def _request_model(
        self,
        *,
        method: str,
        path: str,
        response_model: type[ResponseModel],
        timeout: float,
        body: bytes | None = None,
        requires_csrf: bool = False,
        allow_reauthentication: bool = True,
        successful_statuses: frozenset[int] = frozenset({200}),
    ) -> _TypedModelResult[ResponseModel]:
        for attempt in range(2):
            headers = {"X-CSRF-Token": self._csrf_token()} if requires_csrf else None
            exchange = self._exchange(method, path, body, headers, timeout)
            if exchange.status_code in successful_statuses:
                if _content_type(exchange.headers) != JSON_CONTENT_TYPE:
                    raise ControllerHttpError("controller_http_response_content_type_invalid")
                return _TypedModelResult(
                    model=_decode_model(exchange.body, response_model),
                    headers=exchange.headers,
                )
            error = self._decode_error(exchange)
            if (
                attempt == 0
                and allow_reauthentication
                and error.status_code == 401
                and error.code == "operator_authentication_required"
                and self._reauthenticate is not None
            ):
                self._reauthenticate(self)
                continue
            raise error
        raise ControllerHttpError("controller_http_reauthentication_failed")

    def _request_empty(
        self,
        *,
        method: str,
        path: str,
        timeout: float,
        requires_csrf: bool,
    ) -> None:
        headers = {"X-CSRF-Token": self._csrf_token()} if requires_csrf else None
        exchange = self._exchange(method, path, None, headers, timeout)
        if exchange.status_code != 204:
            raise self._decode_error(exchange)
        if exchange.body:
            raise ControllerHttpError("controller_http_empty_response_invalid")

    @staticmethod
    def _decode_error(exchange: _ExchangeResult) -> ControllerHttpResponseError:
        if _content_type(exchange.headers) != JSON_CONTENT_TYPE:
            raise ControllerHttpError("controller_http_response_content_type_invalid")
        error = _decode_model(exchange.body, ApiErrorResponse)
        allowed_codes = _ERROR_CODES_BY_STATUS.get(exchange.status_code)
        if allowed_codes is None or error.code not in allowed_codes:
            raise ControllerHttpError("controller_http_error_response_invalid")
        return ControllerHttpResponseError(exchange.status_code, error.code)

    def _exchange(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> _ExchangeResult:
        request_headers = {
            "Accept": JSON_CONTENT_TYPE,
            "Accept-Encoding": "identity",
        }
        if body is not None:
            request_headers["Content-Type"] = JSON_CONTENT_TYPE
        if headers is not None:
            request_headers.update(headers)
        url = f"{self._origin}{path}"
        try:
            # manifestで検証済みのHTTPS originと内部固定pathだけを結合する。
            request = Request(  # noqa: S310
                url,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                response: _Response = self._opener.open(request, timeout=timeout)
            except HTTPError as error:
                response = error
        except ControllerHttpError:
            raise
        except (OSError, TimeoutError, URLError, ValueError) as error:
            raise ControllerHttpError("controller_http_transport_failed") from error
        try:
            if response.geturl() != url:
                raise ControllerHttpError("controller_http_redirect_refused")
            content_encoding = response.headers.get("Content-Encoding")
            if content_encoding not in {None, "", "identity"}:
                raise ControllerHttpError("controller_http_response_encoding_invalid")
            expected_length = _content_length(response.headers)
            body_bytes = response.read(MAXIMUM_RESPONSE_BYTES + 1)
            if not isinstance(body_bytes, bytes):
                raise ControllerHttpError("controller_http_response_body_invalid")
            if len(body_bytes) > MAXIMUM_RESPONSE_BYTES:
                raise ControllerHttpError("controller_http_response_too_large")
            if expected_length is not None and expected_length != len(body_bytes):
                raise ControllerHttpError("controller_http_response_length_invalid")
            return _ExchangeResult(
                status_code=response.getcode(),
                headers=response.headers,
                body=body_bytes,
            )
        except (OSError, ValueError) as error:
            raise ControllerHttpError("controller_http_response_read_failed") from error
        finally:
            response.close()


@dataclass(frozen=True, slots=True)
class _TypedModelResult(Generic[ResponseModel]):
    model: ResponseModel
    headers: Message
