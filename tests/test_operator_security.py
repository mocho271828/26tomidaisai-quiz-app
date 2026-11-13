import hashlib
import hmac
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine

from quiz_app.config import AppEnvironment, Settings
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import OperatorAuthService, OperatorPrincipal
from quiz_app.operator.errors import CsrfValidationError, DemoAuthDisabledError
from quiz_app.operator.runtime_isolation import enforce_production_runtime_isolation

ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000002")
CSRF_FIXTURE = "csrf-fixture-value"
INJECTED_PRODUCTION_VALUE = "injected-production-value"


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _auth_service(settings: Settings | None = None) -> OperatorAuthService:
    engine = create_engine("sqlite://")
    return OperatorAuthService(engine, settings or _settings())


def test_password_hash_uses_argon2id() -> None:
    service = _auth_service()
    password = SecretStr("demo-password")
    password_hash = service.hash_password(password)

    assert password_hash.startswith("$argon2id$")
    assert service.password_matches(password_hash, password) is True
    assert service.password_matches(password_hash, SecretStr("different-password")) is False


def test_csrf_requires_cookie_header_and_session_hash_to_match() -> None:
    settings = _settings()
    token = CSRF_FIXTURE
    csrf_hash = hmac.new(
        settings.session_secret.get_secret_value().encode(),
        token.encode(),
        hashlib.sha256,
    ).hexdigest()
    principal = OperatorPrincipal(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        username="operator",
        role=AdminRole.OPERATOR,
        csrf_hash=csrf_hash,
    )
    service = _auth_service(settings)

    service.verify_csrf(principal, token, token)
    with pytest.raises(CsrfValidationError):
        service.verify_csrf(principal, token, "different-token")
    with pytest.raises(CsrfValidationError):
        service.verify_csrf(principal, None, token)


def test_disabled_demo_auth_rejects_existing_session_before_database_access() -> None:
    service = _auth_service(_settings(enable_demo_auth=False))

    with pytest.raises(DemoAuthDisabledError):
        service.authenticate("existing-token")


def test_local_runtime_isolation_does_not_touch_database() -> None:
    unusable_engine = cast(Engine, object())

    enforce_production_runtime_isolation(_settings(), unusable_engine)


def test_production_runtime_isolation_rejects_demo_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def __init__(self, _: Engine) -> None:
            pass

        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def scalar(self, _: object) -> int:
            return 1

    monkeypatch.setattr("quiz_app.operator.runtime_isolation.Session", FakeSession)
    production = _settings(
        app_env=AppEnvironment.PRODUCTION,
        enable_demo_auth=False,
        enable_demo_seed=False,
        session_cookie_secure=True,
        demo_operator_password=INJECTED_PRODUCTION_VALUE,
        session_secret=INJECTED_PRODUCTION_VALUE,
        round_backup_storage_mode="disabled",
        round_backup_directory="/var/empty/quiz-round-backups",
        round_backup_encryption_key="disabled",
        realtime_allowed_origins=["https://quiz.example.test"],
    )

    with pytest.raises(RuntimeError, match="contains_demo_accounts"):
        enforce_production_runtime_isolation(production, cast(Engine, object()))
