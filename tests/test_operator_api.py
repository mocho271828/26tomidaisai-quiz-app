from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from quiz_app.config import (
    OPERATOR_CSRF_COOKIE_NAME,
    OPERATOR_SESSION_COOKIE_NAME,
    Settings,
    get_settings,
)
from quiz_app.main import app
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import (
    CreatedOperatorSession,
    OperatorPrincipal,
    get_operator_auth_service,
)
from quiz_app.operator.errors import CsrfValidationError, OperatorSessionInvalidError

ACCOUNT_ID = UUID("10000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("10000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
NEW_SESSION_FIXTURE = "new-session-value"
VALID_SESSION_FIXTURE = "valid-session-value"
OLD_SESSION_FIXTURE = "old-session-value"
CSRF_FIXTURE = "csrf-fixture-value"


def _principal() -> OperatorPrincipal:
    return OperatorPrincipal(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        username="demo-operator",
        role=AdminRole.OPERATOR,
        csrf_hash="stored-csrf-hash",
    )


class StubAuthService:
    def __init__(self) -> None:
        self.login_username: str | None = None
        self.existing_session_token: str | None = None
        self.logged_out_session_id: UUID | None = None

    def login(
        self,
        username: str,
        _: SecretStr,
        existing_session_token: str | None,
    ) -> CreatedOperatorSession:
        self.login_username = username
        self.existing_session_token = existing_session_token
        return CreatedOperatorSession(
            principal=_principal(),
            session_token=NEW_SESSION_FIXTURE,
            csrf_token=CSRF_FIXTURE,
            expires_at=NOW + timedelta(hours=8),
        )

    def authenticate(self, session_token: str) -> OperatorPrincipal:
        if session_token != VALID_SESSION_FIXTURE:
            raise OperatorSessionInvalidError("invalid")
        return _principal()

    def verify_csrf(
        self,
        _: OperatorPrincipal,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> None:
        if csrf_cookie != CSRF_FIXTURE or csrf_header != CSRF_FIXTURE:
            raise CsrfValidationError("invalid")

    def logout(self, session_id: UUID) -> None:
        self.logged_out_session_id = session_id


@pytest_asyncio.fixture
async def operator_client() -> AsyncIterator[tuple[AsyncClient, StubAuthService]]:
    auth_service = StubAuthService()
    app.dependency_overrides[get_operator_auth_service] = lambda: auth_service
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client, auth_service
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_rotates_session_and_sets_scoped_secure_attributes(
    operator_client: tuple[AsyncClient, StubAuthService],
) -> None:
    client, auth_service = operator_client
    client.cookies.set(OPERATOR_SESSION_COOKIE_NAME, OLD_SESSION_FIXTURE, path="/api/v1/operator")

    response = await client.post(
        "/api/v1/operator/auth/login",
        json={"username": "  demo-operator  ", "password": "local-demo-only"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "operator": {
            "account_id": str(ACCOUNT_ID),
            "username": "demo-operator",
            "role": "Operator",
        }
    }
    assert auth_service.login_username == "demo-operator"
    assert auth_service.existing_session_token == OLD_SESSION_FIXTURE
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(
        value for value in cookies if value.startswith(OPERATOR_SESSION_COOKIE_NAME)
    )
    csrf_cookie = next(value for value in cookies if value.startswith(OPERATOR_CSRF_COOKIE_NAME))
    assert "HttpOnly" in session_cookie
    assert "Path=/api/v1/operator" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Path=/" in csrf_cookie
    assert NEW_SESSION_FIXTURE not in response.text
    assert CSRF_FIXTURE not in response.text


@pytest.mark.asyncio
async def test_session_requires_opaque_cookie(
    operator_client: tuple[AsyncClient, StubAuthService],
) -> None:
    client, _ = operator_client

    response = await client.get("/api/v1/operator/auth/session")

    assert response.status_code == 401
    assert response.json()["code"] == "operator_authentication_required"


@pytest.mark.asyncio
async def test_validation_error_does_not_echo_plaintext_password(
    operator_client: tuple[AsyncClient, StubAuthService],
) -> None:
    client, _ = operator_client
    sentinel = "DO-NOT-ECHO-THIS-PASSWORD"

    response = await client.post(
        "/api/v1/operator/auth/login",
        json={"username": "demo-operator", "password": sentinel * 20},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "request_validation_failed",
        "message": "入力内容を確認してください。",
    }
    assert sentinel not in response.text


@pytest.mark.asyncio
async def test_secure_cookie_setting_and_logout_revoke_session(
    operator_client: tuple[AsyncClient, StubAuthService],
) -> None:
    client, auth_service = operator_client
    app.dependency_overrides[get_settings] = lambda: Settings(
        session_cookie_secure=True,
        _env_file=None,
    )

    login_response = await client.post(
        "/api/v1/operator/auth/login",
        json={"username": "demo-operator", "password": "local-demo-only"},
    )
    assert all("Secure" in value for value in login_response.headers.get_list("set-cookie"))

    logout_response = await client.post(
        "/api/v1/operator/auth/logout",
        headers={
            "Cookie": (
                f"{OPERATOR_SESSION_COOKIE_NAME}={VALID_SESSION_FIXTURE}; "
                f"{OPERATOR_CSRF_COOKIE_NAME}={CSRF_FIXTURE}"
            ),
            "X-CSRF-Token": CSRF_FIXTURE,
        },
    )

    assert logout_response.status_code == 204
    assert auth_service.logged_out_session_id == SESSION_ID
    deleted = logout_response.headers.get_list("set-cookie")
    assert len(deleted) == 2
    assert all("Max-Age=0" in value and "Secure" in value for value in deleted)


def test_openapi_uses_typed_validation_error_without_password_echo_contract() -> None:
    schema = app.openapi()
    login_validation_schema = schema["paths"]["/api/v1/operator/auth/login"]["post"]["responses"][
        "422"
    ]["content"]["application/json"]["schema"]
    assert login_validation_schema == {"$ref": "#/components/schemas/ApiErrorResponse"}
    session_operation = schema["paths"]["/api/v1/operator/auth/session"]["get"]
    assert session_operation["security"] == [{"OperatorSessionCookie": []}]
