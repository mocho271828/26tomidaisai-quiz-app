import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from time import monotonic
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlmodel import Session

from quiz_app.config import AppEnvironment, Settings, get_settings
from quiz_app.db import get_engine
from quiz_app.models.enums import AdminRole
from quiz_app.models.identity import AdminAccount, AdminSession
from quiz_app.operator.errors import (
    CsrfValidationError,
    DemoAuthDisabledError,
    InvalidOperatorCredentialsError,
    OperatorSessionInvalidError,
)

OPERATOR_PASSWORD_TIME_COST = 3
OPERATOR_PASSWORD_MEMORY_COST = 65_536
OPERATOR_PASSWORD_PARALLELISM = 4
OPERATOR_PASSWORD_HASH_LENGTH = 32
OPERATOR_PASSWORD_SALT_LENGTH = 16


def create_operator_password_hasher() -> PasswordHasher:
    """運営認証と配置前検証で同一のArgon2id policyを使用する。"""

    return PasswordHasher(
        time_cost=OPERATOR_PASSWORD_TIME_COST,
        memory_cost=OPERATOR_PASSWORD_MEMORY_COST,
        parallelism=OPERATOR_PASSWORD_PARALLELISM,
        hash_len=OPERATOR_PASSWORD_HASH_LENGTH,
        salt_len=OPERATOR_PASSWORD_SALT_LENGTH,
        type=Type.ID,
    )


def is_current_operator_password_hash(password_hash: str) -> bool:
    """encoded hashがparse可能で現行Argon2id policyと一致するか判定する。"""

    try:
        return not create_operator_password_hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    account_id: UUID
    session_id: UUID
    username: str
    role: AdminRole
    csrf_hash: str


@dataclass(frozen=True, slots=True)
class CreatedOperatorSession:
    principal: OperatorPrincipal
    session_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedOperatorAuthentication:
    principal: OperatorPrincipal
    database_time: datetime
    expires_at: datetime
    verified_from: float


class OperatorAuthService:
    """Argon2id credentialとPostgreSQL sessionを扱う。"""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._password_hasher = create_operator_password_hasher()
        self._dummy_password_hash = self._password_hasher.hash("local-dummy-credential")

    def hash_password(self, password: SecretStr) -> str:
        """seed時も同じArgon2id policyでhashを生成する。"""

        return self._password_hasher.hash(password.get_secret_value())

    def password_matches(self, password_hash: str, password: SecretStr) -> bool:
        """既存hashを同じArgon2id policyで検証し、秘密値を外へ出さない。"""

        return self._verify_password(password_hash, password)

    def login(
        self,
        username: str,
        password: SecretStr,
        existing_session_token: str | None,
    ) -> CreatedOperatorSession:
        """demo accountを検証し、固定化されていない新sessionを発行する。"""

        if (
            not self._settings.enable_demo_auth
            or self._settings.app_env is AppEnvironment.PRODUCTION
        ):
            raise DemoAuthDisabledError("demo_auth_disabled")

        with Session(self._engine) as session, session.begin():
            account = session.scalar(
                select(AdminAccount).where(
                    AdminAccount.username == username,
                    AdminAccount.is_demo.is_(True),
                    AdminAccount.is_revoked.is_(False),
                )
            )
            password_hash = (
                account.password_hash if account is not None else self._dummy_password_hash
            )
            if not self._verify_password(password_hash, password):
                raise InvalidOperatorCredentialsError("invalid_operator_credentials")
            if account is None:
                raise InvalidOperatorCredentialsError("invalid_operator_credentials")

            database_time = self._database_time(session)
            if existing_session_token is not None:
                existing_hash = self._hash_token(existing_session_token)
                existing_session = session.scalar(
                    select(AdminSession).where(AdminSession.token_hash == existing_hash)
                )
                if existing_session is not None:
                    existing_session.revoked_at = database_time

            session_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            expires_at = database_time + timedelta(
                minutes=self._settings.operator_session_ttl_minutes
            )
            admin_session = AdminSession(
                account_id=account.id,
                token_hash=self._hash_token(session_token),
                csrf_hash=self._hash_token(csrf_token),
                issued_at=database_time,
                expires_at=expires_at,
                last_action_at=database_time,
            )
            session.add(admin_session)
            return CreatedOperatorSession(
                principal=OperatorPrincipal(
                    account_id=account.id,
                    session_id=admin_session.id,
                    username=account.username,
                    role=account.role,
                    csrf_hash=admin_session.csrf_hash,
                ),
                session_token=session_token,
                csrf_token=csrf_token,
                expires_at=expires_at,
            )

    def authenticate(self, session_token: str) -> OperatorPrincipal:
        """opaque token hashから有効なaccount/sessionだけを復元する。"""

        if (
            not self._settings.enable_demo_auth
            or self._settings.app_env is AppEnvironment.PRODUCTION
        ):
            raise DemoAuthDisabledError("demo_auth_disabled")

        with Session(self._engine) as session, session.begin():
            database_time = self._database_time(session)
            row = session.execute(
                select(AdminSession, AdminAccount)
                .join(AdminAccount, AdminAccount.id == AdminSession.account_id)
                .where(
                    AdminSession.token_hash == self._hash_token(session_token),
                    AdminSession.revoked_at.is_(None),
                    AdminSession.expires_at > database_time,
                    AdminAccount.is_demo.is_(True),
                    AdminAccount.is_revoked.is_(False),
                )
            ).one_or_none()
            if row is None:
                raise OperatorSessionInvalidError("operator_session_invalid")
            admin_session, account = row
            admin_session.last_action_at = database_time
            return OperatorPrincipal(
                account_id=account.id,
                session_id=admin_session.id,
                username=account.username,
                role=account.role,
                csrf_hash=admin_session.csrf_hash,
            )

    def authenticate_realtime(self, session_token: str) -> VerifiedOperatorAuthentication:
        """WS配信前の確認はlast_action_atを更新せず、絶対満了を返す。"""

        if (
            not self._settings.enable_demo_auth
            or self._settings.app_env is AppEnvironment.PRODUCTION
        ):
            raise DemoAuthDisabledError("demo_auth_disabled")
        verified_from = monotonic()
        with Session(self._engine) as session:
            row = session.execute(
                select(AdminSession, AdminAccount, func.clock_timestamp())
                .join(AdminAccount, AdminAccount.id == AdminSession.account_id)
                .where(
                    AdminSession.token_hash == self._hash_token(session_token),
                    AdminSession.revoked_at.is_(None),
                    AdminSession.expires_at > func.clock_timestamp(),
                    AdminAccount.is_demo.is_(True),
                    AdminAccount.is_revoked.is_(False),
                )
            ).one_or_none()
            if row is None:
                raise OperatorSessionInvalidError("operator_session_invalid")
            admin_session, account, database_time = row
            if not isinstance(database_time, datetime) or admin_session.expires_at <= database_time:
                raise OperatorSessionInvalidError("operator_session_invalid")
            return VerifiedOperatorAuthentication(
                principal=OperatorPrincipal(
                    account_id=account.id,
                    session_id=admin_session.id,
                    username=account.username,
                    role=account.role,
                    csrf_hash=admin_session.csrf_hash,
                ),
                database_time=database_time,
                expires_at=admin_session.expires_at,
                verified_from=verified_from,
            )

    def verify_csrf(
        self,
        principal: OperatorPrincipal,
        csrf_cookie: str | None,
        csrf_header: str | None,
    ) -> None:
        """double-submit値とDB保存hashをconstant-time比較する。"""

        if csrf_cookie is None or csrf_header is None:
            raise CsrfValidationError("csrf_token_missing")
        if not hmac.compare_digest(csrf_cookie, csrf_header):
            raise CsrfValidationError("csrf_double_submit_mismatch")
        if not hmac.compare_digest(self._hash_token(csrf_header), principal.csrf_hash):
            raise CsrfValidationError("csrf_session_mismatch")

    def logout(self, session_id: UUID) -> None:
        """認証済みsessionをDBで失効させる。"""

        with Session(self._engine) as session, session.begin():
            admin_session = session.get(AdminSession, session_id)
            if admin_session is not None and admin_session.revoked_at is None:
                admin_session.revoked_at = self._database_time(session)

    def _hash_token(self, token: str) -> str:
        key = self._settings.session_secret.get_secret_value().encode()
        return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()

    def _verify_password(self, password_hash: str, password: SecretStr) -> bool:
        try:
            return self._password_hasher.verify(password_hash, password.get_secret_value())
        except (InvalidHashError, VerificationError):
            return False

    @staticmethod
    def _database_time(session: Session) -> datetime:
        database_time = session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_time, datetime):
            raise OperatorSessionInvalidError("database_clock_unavailable")
        return database_time


@lru_cache
def get_operator_auth_service() -> OperatorAuthService:
    """process共通の認証serviceを返す。"""

    return OperatorAuthService(get_engine(), get_settings())
