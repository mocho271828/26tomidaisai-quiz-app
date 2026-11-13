import base64
import binascii
import hashlib
import hmac
import re
from enum import StrEnum
from functools import lru_cache
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from pathlib import Path
from socket import inet_aton
from typing import Literal, Self, TypeAlias
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    Field,
    ModelWrapValidatorHandler,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_DEMO_PASSWORD = SecretStr("local-demo-only")
LOCAL_SESSION_SECRET = SecretStr("local-demo-session-secret-change-me")
LOCAL_REVIEW_PASSWORD = SecretStr("replace-for-local-review")
LOCAL_REVIEW_SESSION_SECRET = SecretStr("replace-for-local-review-with-at-least-32-characters")
# 旧配布鍵のpadding有無を問わず、AWS credentialへの再利用を拒否する。
RETIRED_BACKUP_KEY_SHA256 = frozenset(
    {
        "3a14dd2a35c210303e783d931b0e668ded4a9c5f539e7396d743c7a5485fb0a5",
        "edae63aaf5a9b632d806a4e445ad2b94ea27316080b4b7d1800a33c8c1a54854",
    }
)
AWS_DEMO_OPERATOR_USERNAME_SENTINEL = "managed-in-database"
AWS_DEMO_OPERATOR_PASSWORD_SENTINEL = SecretStr("unused")
AWS_DEMO_POSTGRES_CA_CONTAINER_PATH = Path("/run/secrets/aws-demo-postgres-ca.pem")
AWS_DEMO_DISABLED_BACKUP_DIRECTORY = Path("/var/empty/quiz-round-backups")
ROUND_BACKUP_FILESYSTEM_DIRECTORY = Path("/var/lib/quiz-round-backups")
ROUND_BACKUP_DISABLED_SENTINEL = "disabled"
ROUND_BACKUP_STORAGE_MODE_DISABLED = "disabled"
ROUND_BACKUP_STORAGE_MODE_FILESYSTEM = "filesystem"
GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
EVENT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
API_INSTANCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
LOCAL_DEMO_EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
AWS_DEMO_SESSION_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}")
PUBLIC_SECRET_MARKERS = (
    "change-me",
    "credential",
    "csrf",
    "dummy",
    "fixture",
    "injected",
    "local-demo",
    "local-only",
    "must-not-appear",
    "password",
    "placeholder",
    "replace-for",
    "secret",
    "session",
    "token",
)
LOCAL_SERVICE_HOSTS = frozenset(
    {
        "gateway.docker.internal",
        "host.docker.internal",
        "localstack",
        "postgres",
        "redis",
    }
)
PLACEHOLDER_HOST_SUFFIXES = (".example", ".invalid", ".test")
RESERVED_PLACEHOLDER_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
AWS_PRIVATE_IPV4_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
AWS_PRIVATE_IPV6_NETWORK = IPv6Network("fc00::/7")
LOCAL_DATABASE_USERNAMES = frozenset({"postgres", "quiz"})
LOCAL_DATABASE_NAMES = frozenset({"postgres", "quiz_demo"})
LOCAL_DATABASE_PASSWORDS = frozenset({"local-only-database-password", "quiz"})
LOCAL_REDIS_PASSWORDS = frozenset({"local-only-redis-password"})
PUBLIC_REPOSITORY_SECRET_VALUES = (
    LOCAL_DEMO_PASSWORD.get_secret_value(),
    LOCAL_SESSION_SECRET.get_secret_value(),
    LOCAL_REVIEW_PASSWORD.get_secret_value(),
    LOCAL_REVIEW_SESSION_SECRET.get_secret_value(),
    *sorted(LOCAL_DATABASE_PASSWORDS),
    *sorted(LOCAL_REDIS_PASSWORDS),
)
OPERATOR_SESSION_COOKIE_NAME = "quiz_operator_session"
OPERATOR_CSRF_COOKIE_NAME = "quiz_operator_csrf"
PARTICIPANT_SESSION_COOKIE_NAME = "quiz_participant_session"

RoundBackupStorageMode: TypeAlias = Literal["disabled", "filesystem"]


def is_loopback_origin_host(host: str | None) -> bool:
    """URL parser差を正規化しlocalhostとloopback IPを判定する。"""

    if host is None:
        return True
    normalized_host = host.lower().rstrip(".")
    if normalized_host.startswith("[") and normalized_host.endswith("]"):
        normalized_host = normalized_host[1:-1]
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        return True
    try:
        parsed_address = ip_address(normalized_host)
    except ValueError:
        return False
    if parsed_address.is_loopback or parsed_address.is_unspecified:
        return True
    return (
        isinstance(parsed_address, IPv6Address)
        and parsed_address.ipv4_mapped is not None
        and (parsed_address.ipv4_mapped.is_loopback or parsed_address.ipv4_mapped.is_unspecified)
    )


def is_local_service_host(host: str | None) -> bool:
    """接続先として危険なlocal・予約・非unicast hostを判定する。"""

    if host is None or is_loopback_origin_host(host):
        return True
    normalized_host = host.lower().rstrip(".")
    try:
        parsed_address = ip_address(normalized_host)
    except ValueError:
        try:
            legacy_ipv4_address = IPv4Address(inet_aton(normalized_host))
        except OSError:
            legacy_ipv4_address = None
        if legacy_ipv4_address is not None:
            return not is_allowed_aws_remote_address(legacy_ipv4_address)
        return (
            normalized_host in LOCAL_SERVICE_HOSTS
            or normalized_host in RESERVED_PLACEHOLDER_DOMAINS
            or any(
                normalized_host.endswith(f".{domain}") for domain in RESERVED_PLACEHOLDER_DOMAINS
            )
            or normalized_host.endswith(".local")
            or normalized_host.endswith(PLACEHOLDER_HOST_SUFFIXES)
            or "." not in normalized_host
        )
    return not is_allowed_aws_remote_address(parsed_address)


def is_allowed_aws_remote_address(address: IPv4Address | IPv6Address) -> bool:
    """構造上許可するglobal unicastまたはRFC private addressだけを判定する。"""

    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return is_allowed_aws_remote_address(address.ipv4_mapped)
    if isinstance(address, IPv4Address):
        if any(address in network for network in AWS_PRIVATE_IPV4_NETWORKS):
            return True
    elif address in AWS_PRIVATE_IPV6_NETWORK:
        return True
    return address.is_global and not address.is_multicast


def is_public_repository_secret(value: str) -> bool:
    """repositoryで公開済みの既知secret値を全件比較して判定する。"""

    encoded_value = value.encode("utf-8")
    is_match = 0
    for public_value in PUBLIC_REPOSITORY_SECRET_VALUES:
        is_match |= int(hmac.compare_digest(encoded_value, public_value.encode("utf-8")))
    digest = hashlib.sha256(encoded_value.removesuffix(b"=")).hexdigest()
    return bool(is_match) or digest in RETIRED_BACKUP_KEY_SHA256


def is_strong_aws_demo_secret(value: str, *, minimum_length: int) -> bool:
    """ASCII長・文字多様性と既知弱値を拒否する（entropyは証明しない）。"""

    normalized_value = value.casefold()
    is_public_value = is_public_repository_secret(value)
    return (
        value.isascii()
        and len(value) >= minimum_length
        and len(set(value)) >= 8
        and not any(marker in normalized_value for marker in PUBLIC_SECRET_MARKERS)
        and not is_public_value
    )


def has_short_repeating_period(value: bytes, *, maximum_period: int = 16) -> bool:
    """既知の自明な反復byte列を検出する（entropyの証明は行わない）。"""

    return any(
        len(value) % period == 0 and value == value[:period] * (len(value) // period)
        for period in range(1, min(maximum_period, len(value)) + 1)
    )


def is_valid_aws_demo_database_url(database_url: str) -> bool:
    """AWS demo用PostgreSQL接続がTLS検証付きremote接続かを判定する。"""

    try:
        parsed_url = urlsplit(database_url)
        hostname = unquote(parsed_url.hostname or "", errors="strict")
        username = unquote(parsed_url.username or "", errors="strict")
        password = unquote(parsed_url.password or "", errors="strict")
        database_name = unquote(parsed_url.path.removeprefix("/"), errors="strict")
        port = parsed_url.port
    except (UnicodeDecodeError, ValueError):
        return False

    query_parameters = parse_qs(parsed_url.query, keep_blank_values=True)
    return (
        database_url.isascii()
        and parsed_url.scheme == "postgresql+psycopg"
        and username not in {"", *LOCAL_DATABASE_USERNAMES}
        and is_strong_aws_demo_secret(password, minimum_length=16)
        and password not in LOCAL_DATABASE_PASSWORDS
        and database_name not in {"", *LOCAL_DATABASE_NAMES}
        and (port is None or port > 0)
        and not is_local_service_host(hostname)
        and query_parameters == {"sslmode": ["verify-full"]}
        and parsed_url.fragment == ""
    )


def is_valid_aws_demo_redis_url(redis_url: str) -> bool:
    """AWS demo用Redis接続が認証付きTLS remote接続かを判定する。"""

    try:
        parsed_url = urlsplit(redis_url)
        hostname = unquote(parsed_url.hostname or "", errors="strict")
        password = unquote(parsed_url.password or "", errors="strict")
        port = parsed_url.port
    except (UnicodeDecodeError, ValueError):
        return False
    return (
        redis_url.isascii()
        and parsed_url.scheme == "rediss"
        and is_strong_aws_demo_secret(password, minimum_length=16)
        and password not in LOCAL_REDIS_PASSWORDS
        and (port is None or port > 0)
        and not is_local_service_host(hostname)
        and parsed_url.query == ""
        and parsed_url.fragment == ""
    )


class AppEnvironment(StrEnum):
    """実行環境。demo機能をproductionへ持ち込まないため列挙する。"""

    LOCAL_DEMO = "local-demo"
    LOCAL_TEST = "local-test"
    AWS_DEMO = "aws-demo"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """環境変数を検証済みの値へ変換する。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    app_env: AppEnvironment = AppEnvironment.LOCAL_DEMO
    app_version: str = "0.1.0"
    database_url: str = Field(
        default="postgresql+psycopg://quiz:quiz@postgres:5432/quiz_demo",
        repr=False,
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=0, ge=0, le=100)
    database_sync_pool_size: int = Field(default=3, ge=2, le=100)
    database_sync_max_overflow: int = Field(default=0, ge=0, le=100)
    database_result_worker_pool_size: int = Field(default=2, ge=1, le=10)
    database_pool_timeout_seconds: float = Field(default=30, gt=0, le=60)
    redis_url: str = Field(default="redis://redis:6379/0", repr=False)
    demo_operator_username: str = "demo-operator"
    demo_operator_password: SecretStr = LOCAL_DEMO_PASSWORD
    session_secret: SecretStr = LOCAL_SESSION_SECRET
    enable_demo_auth: bool = True
    enable_demo_seed: bool = True
    session_cookie_secure: bool = False
    operator_session_ttl_minutes: int = Field(default=480, ge=15, le=1_440)
    participant_session_ttl_minutes: int = Field(default=720, ge=15, le=1_440)
    participant_join_retry_minutes: int = Field(default=5, ge=1, le=30)
    realtime_allowed_origins: list[AnyHttpUrl] = Field(
        default_factory=lambda: [AnyHttpUrl("http://localhost:8080")],
        min_length=1,
    )
    redis_connect_timeout_seconds: float = Field(default=0.25, gt=0, le=2)
    redis_operation_timeout_seconds: float = Field(default=0.25, gt=0, le=2)
    postgres_ssl_root_cert: Path = Field(
        default=Path("/etc/ssl/certs/ca-certificates.crt"),
        validation_alias="PGSSLROOTCERT",
    )
    aws_demo_expected_event_id: UUID | None = None
    aws_demo_round_scope: (
        Literal["single-round", "rehearsal-six-round", "load-test-ten-round"] | None
    ) = None
    aws_demo_toxiproxy_config_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    api_instance_id: str = Field(default="api-local", pattern=API_INSTANCE_ID_PATTERN)
    logfire_send_to_logfire: bool = False
    round_backup_storage_mode: RoundBackupStorageMode = ROUND_BACKUP_STORAGE_MODE_FILESYSTEM
    round_backup_directory: Path = ROUND_BACKUP_FILESYSTEM_DIRECTORY
    round_backup_encryption_key: SecretStr

    @field_validator("realtime_allowed_origins")
    @classmethod
    def validate_realtime_origins(cls, origins: list[AnyHttpUrl]) -> list[AnyHttpUrl]:
        """browser Originと一致しないpathやwildcardを設定段階で拒否する。"""

        for origin in origins:
            if (
                "*" in (origin.host or "")
                or origin.username is not None
                or origin.password is not None
                or origin.path not in {None, "", "/"}
                or origin.query is not None
                or origin.fragment is not None
            ):
                raise ValueError("realtime originはscheme・host・portだけで指定してください")
        return origins

    @field_validator("round_backup_directory")
    @classmethod
    def validate_round_backup_directory(cls, directory: Path) -> Path:
        """backup先を曖昧な相対pathやfilesystem rootにしない。"""

        if not directory.is_absolute() or directory == Path("/"):
            raise ValueError("round backup directoryはroot以外の絶対pathで指定してください")
        return directory

    @field_validator("round_backup_encryption_key")
    @classmethod
    def validate_round_backup_encryption_key(cls, key: SecretStr) -> SecretStr:
        """Fernet鍵またはbackup無効を表す非秘密sentinelだけを受け付ける。"""

        if key.get_secret_value() == ROUND_BACKUP_DISABLED_SENTINEL:
            return key
        try:
            decoded = base64.b64decode(
                key.get_secret_value().encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise ValueError("round backup encryption keyが不正です") from error
        if len(decoded) != 32:
            raise ValueError("round backup encryption keyは32byteで指定してください")
        if has_short_repeating_period(decoded):
            raise ValueError("round backup encryption keyに単純反復値は使用できません")
        return key

    @model_validator(mode="after")
    def validate_environment_isolation(self) -> Self:
        """remote環境がlocal設定や不十分な接続保護で起動することを防ぐ。"""

        backup_key = self.round_backup_encryption_key.get_secret_value()
        if self.app_env is AppEnvironment.LOCAL_DEMO:
            self._require_filesystem_round_backup("local-demo")
            return self
        if self.app_env is AppEnvironment.LOCAL_TEST:
            self._require_disabled_round_backup("local-test")
            return self
        if self.app_env is AppEnvironment.AWS_DEMO:
            return self.validate_aws_demo_configuration()

        has_default_password = self.demo_operator_password.get_secret_value() in {
            LOCAL_DEMO_PASSWORD.get_secret_value(),
            LOCAL_REVIEW_PASSWORD.get_secret_value(),
        }
        has_default_session_secret = self.session_secret.get_secret_value() in {
            LOCAL_SESSION_SECRET.get_secret_value(),
            LOCAL_REVIEW_SESSION_SECRET.get_secret_value(),
        }
        has_default_backup_key = is_public_repository_secret(backup_key)
        if (
            self.enable_demo_auth
            or self.enable_demo_seed
            or not self.session_cookie_secure
            or has_default_password
            or has_default_session_secret
            or has_default_backup_key
        ):
            raise ValueError(
                "productionではdemo機能とlocal既定secret・backup鍵を無効化してください"
            )
        self._require_disabled_round_backup("production")
        if any(
            origin.scheme != "https" or is_loopback_origin_host(origin.host)
            for origin in self.realtime_allowed_origins
        ):
            raise ValueError("productionではlocal以外のHTTPS realtime originが必要です")
        return self

    @model_validator(mode="wrap")
    @classmethod
    def redact_validation_error_inputs(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        """設定検証例外から接続credentialを含む入力値を除去する。"""

        try:
            return handler(value)
        except ValidationError as error:
            raise ValidationError.from_exception_data(
                title=error.title,
                line_errors=error.errors(include_input=False),
                hide_input=True,
            ) from None

    def validate_aws_demo_configuration(self) -> Self:
        """AWS demoに必要な認証・TLS・remote接続・artifact識別をfail-closedで検証する。"""

        if "enable_demo_auth" not in self.model_fields_set or not self.enable_demo_auth:
            raise ValueError("AWS demoではdemo認証を有効化してください")
        if self.enable_demo_seed:
            raise ValueError("AWS demoではdemo seedを無効化してください")
        if not self.session_cookie_secure:
            raise ValueError("AWS demoではsecure session cookieが必要です")
        if self.operator_session_ttl_minutes > 120 or self.participant_session_ttl_minutes > 120:
            raise ValueError("AWS demoのsession TTLは120分以下にしてください")
        if self.logfire_send_to_logfire:
            raise ValueError("AWS demo preflightではLogfire外部送信を無効化してください")

        operator_password = self.demo_operator_password.get_secret_value()
        session_secret = self.session_secret.get_secret_value()
        backup_key = self.round_backup_encryption_key.get_secret_value()
        has_default_password = operator_password in {
            LOCAL_DEMO_PASSWORD.get_secret_value(),
            LOCAL_REVIEW_PASSWORD.get_secret_value(),
        }
        has_default_session_secret = session_secret in {
            LOCAL_SESSION_SECRET.get_secret_value(),
            LOCAL_REVIEW_SESSION_SECRET.get_secret_value(),
        }
        has_public_session_secret = is_public_repository_secret(session_secret)
        has_default_backup_key = is_public_repository_secret(backup_key)
        if (
            has_default_password
            or has_default_session_secret
            or has_public_session_secret
            or has_default_backup_key
        ):
            raise ValueError("AWS demoではrepository公開済みのlocal placeholderを利用できません")
        if self.aws_demo_round_scope == "single-round":
            self._require_disabled_round_backup("AWS demo single-round")
        elif self.aws_demo_round_scope in {"rehearsal-six-round", "load-test-ten-round"}:
            self._require_filesystem_round_backup(f"AWS demo {self.aws_demo_round_scope}")
            if not is_strong_aws_demo_secret(backup_key, minimum_length=43):
                raise ValueError("AWSのRound backupには既知・単純値でない未公開Fernet鍵が必要です")
        else:
            raise ValueError("AWS demoでは明示的なRound scopeが必要です")
        if (
            AWS_DEMO_SESSION_SECRET_PATTERN.fullmatch(session_secret) is None
            or not is_strong_aws_demo_secret(session_secret, minimum_length=43)
            or not any(character.islower() for character in session_secret)
            or not any(character.isupper() for character in session_secret)
            or not any(character.isdigit() for character in session_secret)
        ):
            raise ValueError(
                "AWS demoのsession secretは43〜128文字の十分に多様なURL-safe ASCII値が必要です"
            )
        if (
            self.demo_operator_username != AWS_DEMO_OPERATOR_USERNAME_SENTINEL
            or operator_password != AWS_DEMO_OPERATOR_PASSWORD_SENTINEL.get_secret_value()
        ):
            raise ValueError(
                "AWS demoのoperator credentialはruntime envでなくDB管理sentinelにしてください"
            )
        if GIT_SHA_PATTERN.fullmatch(self.app_version) is None:
            raise ValueError("AWS demoのAPP_VERSIONには40桁lowercase git SHAが必要です")
        if self.postgres_ssl_root_cert != AWS_DEMO_POSTGRES_CA_CONTAINER_PATH:
            raise ValueError("AWS demoのPGSSLROOTCERTには固定container CA pathが必要です")
        if self.aws_demo_expected_event_id == LOCAL_DEMO_EVENT_ID:
            raise ValueError("AWS demoではlocal demo Event IDを利用できません")
        if any(
            origin.scheme != "https" or is_local_service_host(origin.host)
            for origin in self.realtime_allowed_origins
        ):
            raise ValueError("AWS demoではloopback以外のHTTPS realtime originが必要です")
        if not is_valid_aws_demo_database_url(self.database_url):
            raise ValueError(
                "AWS demoのDATABASE_URLには認証・remote host・sslmode=verify-fullが必要です"
            )
        if not is_valid_aws_demo_redis_url(self.redis_url):
            raise ValueError("AWS demoのREDIS_URLには認証付きrediss remote接続が必要です")
        return self

    def _require_disabled_round_backup(self, environment_name: str) -> None:
        """backup無効環境のmode・path・keyを一体で固定する。"""

        if (
            self.round_backup_storage_mode != ROUND_BACKUP_STORAGE_MODE_DISABLED
            or self.round_backup_directory != AWS_DEMO_DISABLED_BACKUP_DIRECTORY
            or self.round_backup_encryption_key.get_secret_value() != ROUND_BACKUP_DISABLED_SENTINEL
        ):
            raise ValueError(
                f"{environment_name}ではRound backupをdisabled mode・固定path・sentinel鍵に"
                "してください"
            )

    def _require_filesystem_round_backup(self, environment_name: str) -> None:
        """filesystem backup環境のmode・container path・Fernet鍵を一体で固定する。"""

        if self.round_backup_storage_mode != ROUND_BACKUP_STORAGE_MODE_FILESYSTEM:
            raise ValueError(f"{environment_name}ではRound backupにfilesystem modeが必要です")
        if self.round_backup_directory != ROUND_BACKUP_FILESYSTEM_DIRECTORY:
            raise ValueError(f"{environment_name}のRound backup directoryが固定pathと不一致です")
        if self.round_backup_encryption_key.get_secret_value() == ROUND_BACKUP_DISABLED_SENTINEL:
            raise ValueError(f"{environment_name}のRound backupにはFernet鍵が必要です")


@lru_cache
def get_settings() -> Settings:
    """process内で検証済み設定を共有する。"""

    return Settings()
