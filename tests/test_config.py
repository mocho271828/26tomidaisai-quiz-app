import base64
import hashlib
import secrets

import pytest
from pydantic import ValidationError

from quiz_app.config import AppEnvironment, Settings

TEST_BACKUP_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
AWS_DEMO_GIT_SHA = "0123456789abcdef0123456789abcdef01234567"
VALID_AWS_URL_CREDENTIAL = f"Aa0_{secrets.token_urlsafe(32)}"


def create_aws_demo_settings(**overrides: object) -> Settings:
    """AWS demoの有効な基準設定から拒否条件だけを差し替える。"""

    injected_value = f"Aa0_{secrets.token_urlsafe(32)}"
    configuration: dict[str, object] = {
        "app_env": AppEnvironment.AWS_DEMO,
        "app_version": AWS_DEMO_GIT_SHA,
        "database_url": (
            "postgresql+psycopg://aws_demo_user:"
            f"{injected_value}@db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        "redis_url": f"rediss://aws_demo_user:{injected_value}@cache.aws.internal:6379/0",
        "demo_operator_username": "managed-in-database",
        "demo_operator_password": "unused",
        "session_secret": injected_value,
        "enable_demo_auth": True,
        "enable_demo_seed": False,
        "session_cookie_secure": True,
        "operator_session_ttl_minutes": 120,
        "participant_session_ttl_minutes": 120,
        "realtime_allowed_origins": ["https://quiz.aws.internal"],
        "postgres_ssl_root_cert": "/run/secrets/aws-demo-postgres-ca.pem",
        "api_instance_id": "api-single",
        "round_backup_storage_mode": "disabled",
        "round_backup_directory": "/var/empty/quiz-round-backups",
        "round_backup_encryption_key": "disabled",
        "aws_demo_expected_event_id": "70000000-0000-4000-8000-000000000001",
        "aws_demo_round_scope": "single-round",
        "_env_file": None,
    }
    configuration.update(overrides)
    return Settings(**configuration)


def test_local_demo_requires_persistent_backup_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROUND_BACKUP_ENCRYPTION_KEY")
    with pytest.raises(ValidationError, match="round_backup_encryption_key"):
        Settings(_env_file=None)


def test_local_demo_defaults_are_accepted_with_backup_key() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env is AppEnvironment.LOCAL_DEMO
    assert settings.enable_demo_auth is True


def test_aws_demo_accepts_only_explicit_remote_configuration() -> None:
    settings = create_aws_demo_settings()

    assert settings.app_env is AppEnvironment.AWS_DEMO
    assert settings.enable_demo_auth is True
    assert settings.enable_demo_seed is False
    assert settings.session_cookie_secure is True
    assert settings.round_backup_storage_mode == "disabled"
    assert settings.round_backup_encryption_key.get_secret_value() == "disabled"
    assert str(settings.postgres_ssl_root_cert) == "/run/secrets/aws-demo-postgres-ca.pem"


def test_aws_demo_requires_explicit_demo_auth_setting() -> None:
    configuration = create_aws_demo_settings().model_dump()
    del configuration["enable_demo_auth"]

    with pytest.raises(ValidationError, match="demo認証"):
        Settings(**configuration, _env_file=None)


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value", "error_message"),
    [
        ("enable_demo_auth", False, "demo認証"),
        ("enable_demo_seed", True, "demo seed"),
        ("session_cookie_secure", False, "secure session cookie"),
        ("round_backup_storage_mode", "filesystem", "disabled mode"),
        ("logfire_send_to_logfire", True, "Logfire外部送信"),
    ],
)
def test_aws_demo_rejects_unsafe_feature_flags(
    setting_name: str,
    unsafe_value: object,
    error_message: str,
) -> None:
    with pytest.raises(ValidationError, match=error_message):
        create_aws_demo_settings(**{setting_name: unsafe_value})


@pytest.mark.parametrize(
    ("setting_name", "placeholder"),
    [
        ("demo_operator_password", "local-demo-only"),
        ("demo_operator_password", "replace-for-local-review"),
        ("session_secret", "local-demo-session-secret-change-me"),
        (
            "session_secret",
            "replace-for-local-review-with-at-least-32-characters",
        ),
    ],
)
def test_aws_demo_rejects_repository_placeholders(
    setting_name: str,
    placeholder: str,
) -> None:
    with pytest.raises(ValidationError, match="local placeholder"):
        create_aws_demo_settings(**{setting_name: placeholder})


@pytest.mark.parametrize("padded", [False, True])
@pytest.mark.parametrize(
    ("setting_name", "error_message"),
    [
        ("session_secret", "local placeholder"),
        ("database_url", "DATABASE_URL"),
        ("redis_url", "REDIS_URL"),
    ],
)
def test_aws_demo_rejects_public_backup_key_reused_as_runtime_secret(
    monkeypatch: pytest.MonkeyPatch,
    padded: bool,
    setting_name: str,
    error_message: str,
) -> None:
    unpadded_key = TEST_BACKUP_KEY.removesuffix("=")
    digest = hashlib.sha256(unpadded_key.encode()).hexdigest()
    monkeypatch.setattr("quiz_app.config.RETIRED_BACKUP_KEY_SHA256", {digest})
    repository_secret = TEST_BACKUP_KEY if padded else unpadded_key
    encoded_secret = repository_secret.replace("=", "%3D")
    unsafe_values = {
        "session_secret": repository_secret,
        "database_url": (
            "postgresql+psycopg://aws_demo_user:"
            f"{encoded_secret}@db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        "redis_url": f"rediss://aws_demo_user:{encoded_secret}@cache.aws.internal:6379/0",
    }

    with pytest.raises(ValidationError, match=error_message):
        create_aws_demo_settings(**{setting_name: unsafe_values[setting_name]})


@pytest.mark.parametrize(
    "repository_fixture",
    [
        "injected-production-value",
        "csrf-fixture-value",
        "control-csrf-value",
        "valid-participant-session",
        "local-dummy-credential",
        "database-secret-must-not-leak",
    ],
)
@pytest.mark.parametrize("setting_name", ["database_url", "redis_url"])
def test_aws_demo_rejects_published_credential_fixture_in_connection_url(
    repository_fixture: str,
    setting_name: str,
) -> None:
    unsafe_values = {
        "database_url": (
            "postgresql+psycopg://aws_demo_user:"
            f"{repository_fixture}@db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        "redis_url": f"rediss://aws_demo_user:{repository_fixture}@cache.aws.internal:6379/0",
    }

    with pytest.raises(ValidationError, match="DATABASE_URL|REDIS_URL"):
        create_aws_demo_settings(**{setting_name: unsafe_values[setting_name]})


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value", "error_message"),
    [
        ("session_secret", "Aa0_" + ("x" * 38), "43〜128文字"),
        ("demo_operator_username", "ab", "DB管理sentinel"),
        ("demo_operator_password", "x" * 16, "DB管理sentinel"),
        ("round_backup_encryption_key", TEST_BACKUP_KEY, "disabled mode"),
        ("postgres_ssl_root_cert", "/etc/invalid-aws-demo-ca.pem", "PGSSLROOTCERT"),
        ("round_backup_directory", "/var/lib/quiz-round-backups", "disabled mode"),
        ("operator_session_ttl_minutes", "121", "session TTL"),
        ("participant_session_ttl_minutes", "121", "session TTL"),
    ],
)
def test_aws_demo_rejects_weak_or_unused_secret_configuration(
    setting_name: str,
    unsafe_value: str,
    error_message: str,
) -> None:
    with pytest.raises(ValidationError, match=error_message):
        create_aws_demo_settings(**{setting_name: unsafe_value})


@pytest.mark.parametrize(
    "username",
    [
        "demo-operator",
        "LOCAL-operator",
        "local_review_operator",
        "review-operator",
        "operator@localhost",
    ],
)
def test_aws_demo_rejects_runtime_operator_identity(username: str) -> None:
    with pytest.raises(ValidationError, match="DB管理sentinel"):
        create_aws_demo_settings(demo_operator_username=username)


@pytest.mark.parametrize(
    "app_version",
    [
        "0.1.0",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
    ],
)
def test_aws_demo_requires_lowercase_full_git_sha(app_version: str) -> None:
    with pytest.raises(ValidationError, match="40桁lowercase git SHA"):
        create_aws_demo_settings(app_version=app_version)


def test_aws_demo_rejects_local_expected_event_id() -> None:
    with pytest.raises(ValidationError, match="local demo Event ID"):
        create_aws_demo_settings(aws_demo_expected_event_id="60000000-0000-4000-8000-000000000001")


@pytest.mark.parametrize(
    "origin",
    [
        "http://quiz.aws.internal",
        "https://localhost",
        "https://127.0.0.1",
        "https://0.0.0.0",
        "https://[::1]",
        "https://[::]",
        "https://[::ffff:127.0.0.1]",
        "https://quiz.example.test",
        "https://quiz.example.com",
        "https://169.254.169.254",
        "https://192.0.2.1",
    ],
)
def test_aws_demo_rejects_non_https_or_local_realtime_origin(origin: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS realtime origin"):
        create_aws_demo_settings(realtime_allowed_origins=[origin])


@pytest.mark.parametrize(
    "database_url",
    [
        (
            "postgresql://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+asyncpg://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        "postgresql+psycopg://aws_demo_user@db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full",
        "postgresql+psycopg://aws_demo_user:@db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full",
        (
            "postgresql+psycopg://aws_demo_user:injected@localhost:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@127.0.0.1:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@127.1:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@0177.0.0.1:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        ("postgresql+psycopg://aws_demo_user:injected@[::]:5432/quiz_aws_demo?sslmode=verify-full"),
        (
            "postgresql+psycopg://aws_demo_user:injected@postgres:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo",
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=require"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full&sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full&host=localhost"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@"
            "%31%32%37.0.0.1:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:invalid/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full#fragment"
        ),
        (
            "postgresql+psycopg://%71uiz:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:local%2Donly%2Ddatabase%2Dpassword@"
            "db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/%71uiz_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://quiz:injected@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:local-only-database-password@"
            "db.aws.internal:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.aws.internal:5432/quiz_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@db.example.test:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@"
            "db.example.com:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@"
            "169.254.169.254:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:injected@"
            "192.0.2.1:5432/quiz_aws_demo?sslmode=verify-full"
        ),
        (
            "postgresql+psycopg://aws_demo_user:short@db.aws.internal:5432/quiz_aws_demo"
            "?sslmode=verify-full"
        ),
    ],
)
def test_aws_demo_rejects_unsafe_database_url(database_url: str) -> None:
    database_url = database_url.replace("injected", VALID_AWS_URL_CREDENTIAL)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        create_aws_demo_settings(database_url=database_url)


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://:injected@cache.aws.internal:6379/0",
        "rediss://cache.aws.internal:6379/0",
        "rediss://:@cache.aws.internal:6379/0",
        "rediss://:injected@localhost:6379/0",
        "rediss://:injected@127.0.0.1:6379/0",
        "rediss://:injected@127.1:6379/0",
        "rediss://:injected@0177.0.0.1:6379/0",
        "rediss://:injected@[::]:6379/0",
        "rediss://:injected@redis:6379/0",
        "rediss://:injected@cache.aws.internal:invalid/0",
        "rediss://:injected@cache.aws.internal:6379/0#fragment",
        "rediss://:injected@cache.aws.internal:6379/0?ssl_cert_reqs=none",
        "rediss://:injected@%31%32%37.0.0.1:6379/0",
        "rediss://:local%2Donly%2Dredis%2Dpassword@cache.aws.internal:6379/0",
        "rediss://:local-only-redis-password@cache.aws.internal:6379/0",
        "rediss://:injected@cache.example.test:6379/0",
        "rediss://:injected@cache.example.org:6379/0",
        "rediss://:injected@169.254.169.254:6379/0",
        "rediss://:injected@192.0.2.1:6379/0",
        "rediss://:short@cache.aws.internal:6379/0",
    ],
)
def test_aws_demo_rejects_unsafe_redis_url(redis_url: str) -> None:
    redis_url = redis_url.replace("injected", VALID_AWS_URL_CREDENTIAL)
    with pytest.raises(ValidationError, match="REDIS_URL"):
        create_aws_demo_settings(redis_url=redis_url)


@pytest.mark.parametrize(
    ("setting_name", "unsafe_url", "connection_secret"),
    [
        (
            "database_url",
            (
                "postgresql+psycopg://quiz:database-secret-must-not-leak@"
                "db.aws.internal:5432/quiz_aws_demo?sslmode=require"
            ),
            "database-secret-must-not-leak",
        ),
        (
            "redis_url",
            "redis://:redis-secret-must-not-leak@cache.aws.internal:6379/0",
            "redis-secret-must-not-leak",
        ),
    ],
)
def test_aws_demo_validation_error_does_not_expose_connection_secret(
    setting_name: str,
    unsafe_url: str,
    connection_secret: str,
) -> None:
    with pytest.raises(ValidationError) as captured_error:
        create_aws_demo_settings(**{setting_name: unsafe_url})

    rendered_error = "\n".join(
        (
            str(captured_error.value),
            repr(captured_error.value),
            repr(captured_error.value.errors()),
        )
    )
    assert connection_secret not in rendered_error


def test_local_test_accepts_only_explicit_disabled_backup_contract() -> None:
    settings = Settings(
        app_env=AppEnvironment.LOCAL_TEST,
        round_backup_storage_mode="disabled",
        round_backup_directory="/var/empty/quiz-round-backups",
        round_backup_encryption_key="disabled",
        _env_file=None,
    )

    assert settings.round_backup_storage_mode == "disabled"


def test_production_rejects_demo_configuration() -> None:
    with pytest.raises(ValidationError, match="demo機能"):
        Settings(app_env=AppEnvironment.PRODUCTION, _env_file=None)


def test_production_accepts_only_isolated_configuration() -> None:
    injected_value = "injected-outside-the-repository"
    settings = Settings(
        app_env=AppEnvironment.PRODUCTION,
        enable_demo_auth=False,
        enable_demo_seed=False,
        session_cookie_secure=True,
        demo_operator_password=injected_value,
        session_secret=injected_value,
        round_backup_storage_mode="disabled",
        round_backup_directory="/var/empty/quiz-round-backups",
        round_backup_encryption_key="disabled",
        realtime_allowed_origins=["https://quiz.example.test"],
        _env_file=None,
    )

    assert settings.app_env is AppEnvironment.PRODUCTION


@pytest.mark.parametrize(
    ("demo_operator_password", "session_secret"),
    [
        ("replace-for-local-review", "injected-outside-the-repository"),
        (
            "injected-outside-the-repository",
            "replace-for-local-review-with-at-least-32-characters",
        ),
    ],
)
def test_production_rejects_local_review_placeholders(
    demo_operator_password: str,
    session_secret: str,
) -> None:
    with pytest.raises(ValidationError, match="local既定secret"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            enable_demo_auth=False,
            enable_demo_seed=False,
            session_cookie_secure=True,
            demo_operator_password=demo_operator_password,
            session_secret=session_secret,
            round_backup_storage_mode="disabled",
            round_backup_directory="/var/empty/quiz-round-backups",
            round_backup_encryption_key="disabled",
            realtime_allowed_origins=["https://quiz.example.test"],
            _env_file=None,
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8080",
        "https://127.0.0.1",
        "https://[::1]",
        "https://localhost.",
        "https://0.0.0.0",
        "https://[::]",
    ],
)
def test_production_rejects_local_realtime_origin(origin: str) -> None:
    injected_value = "injected-outside-the-repository"

    with pytest.raises(ValidationError, match="HTTPS realtime origin"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            enable_demo_auth=False,
            enable_demo_seed=False,
            session_cookie_secure=True,
            demo_operator_password=injected_value,
            session_secret=injected_value,
            round_backup_storage_mode="disabled",
            round_backup_directory="/var/empty/quiz-round-backups",
            round_backup_encryption_key="disabled",
            realtime_allowed_origins=[origin],
            _env_file=None,
        )


@pytest.mark.parametrize(
    "origin",
    ["https://*.example.test", "https://quiz.example.test/realtime"],
)
def test_realtime_origin_rejects_wildcard_and_path(origin: str) -> None:
    with pytest.raises(ValidationError, match="scheme・host・port"):
        Settings(realtime_allowed_origins=[origin], _env_file=None)


@pytest.mark.parametrize("directory", ["relative/backups", "/"])
def test_round_backup_directory_requires_scoped_absolute_path(directory: str) -> None:
    with pytest.raises(ValidationError, match="root以外の絶対path"):
        Settings(round_backup_directory=directory, _env_file=None)


def test_round_backup_key_requires_fernet_key_shape() -> None:
    with pytest.raises(ValidationError, match="32byte"):
        Settings(round_backup_encryption_key="aW52YWxpZA==", _env_file=None)


def test_round_backup_key_rejects_non_base64_characters() -> None:
    with pytest.raises(ValidationError, match="不正"):
        Settings(round_backup_encryption_key="!" * 44, _env_file=None)


@pytest.mark.parametrize("period", [1, 2, 4, 8, 16])
def test_round_backup_key_rejects_short_repeating_decoded_bytes(period: int) -> None:
    repeated_chunk = bytes(range(period))
    repeated_key = base64.urlsafe_b64encode(repeated_chunk * (32 // period)).decode("ascii")

    with pytest.raises(ValidationError, match="単純反復"):
        Settings(round_backup_encryption_key=repeated_key, _env_file=None)


def test_local_round_backup_is_rejected_outside_local_demo() -> None:
    with pytest.raises(ValidationError, match="disabled mode"):
        Settings(app_env=AppEnvironment.LOCAL_TEST, _env_file=None)


@pytest.mark.parametrize("scope", ["rehearsal-six-round", "load-test-ten-round"])
def test_aws_multi_round_scope_requires_fixed_filesystem_backup_with_strong_key(scope: str) -> None:
    backup_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")

    settings = create_aws_demo_settings(
        aws_demo_round_scope=scope,
        api_instance_id="api-primary",
        round_backup_storage_mode="filesystem",
        round_backup_directory="/var/lib/quiz-round-backups",
        round_backup_encryption_key=backup_key,
    )

    assert settings.round_backup_storage_mode == "filesystem"
    assert settings.round_backup_encryption_key.get_secret_value() == backup_key


@pytest.mark.parametrize("scope", ["rehearsal-six-round", "load-test-ten-round"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"round_backup_storage_mode": "disabled"},
        {"round_backup_directory": "/var/empty/quiz-round-backups"},
        {"round_backup_encryption_key": "disabled"},
    ],
)
def test_aws_multi_round_scope_rejects_incomplete_or_public_backup_contract(
    overrides: dict[str, str],
    scope: str,
) -> None:
    backup_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    configuration = {
        "aws_demo_round_scope": scope,
        "round_backup_storage_mode": "filesystem",
        "round_backup_directory": "/var/lib/quiz-round-backups",
        "round_backup_encryption_key": backup_key,
    }
    configuration.update(overrides)

    with pytest.raises(ValidationError, match="Round backup|Fernet"):
        create_aws_demo_settings(**configuration)


@pytest.mark.parametrize(
    "instance_id",
    ["", "API-primary", "api_primary", "1-primary", "a" * 33],
)
def test_api_instance_id_is_bounded_and_log_safe(instance_id: str) -> None:
    with pytest.raises(ValidationError):
        Settings(api_instance_id=instance_id, _env_file=None)


@pytest.mark.parametrize("scope", ["single-round", "rehearsal-six-round", "load-test-ten-round"])
def test_aws_round_backup_rejects_retired_key(monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    digest = hashlib.sha256(TEST_BACKUP_KEY.removesuffix("=").encode()).hexdigest()
    monkeypatch.setattr("quiz_app.config.RETIRED_BACKUP_KEY_SHA256", {digest})
    with pytest.raises(ValidationError, match="local placeholder"):
        create_aws_demo_settings(
            aws_demo_round_scope=scope,
            round_backup_storage_mode="filesystem",
            round_backup_directory="/var/lib/quiz-round-backups",
            round_backup_encryption_key=TEST_BACKUP_KEY,
        )
