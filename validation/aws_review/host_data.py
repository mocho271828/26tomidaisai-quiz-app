"""確認環境の秘密fileと、所有検証後だけ使う専用DB初期化境界。"""

import base64
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.engine import URL, make_url

from quiz_app.config import AWS_DEMO_SESSION_SECRET_PATTERN, Settings, is_strong_aws_demo_secret
from quiz_app.dependency_hosts import validate_remote_dependency_host
from quiz_app.rehearsal.initialization import APPLICATION_ROLE, RehearsalInitializationSecrets
from quiz_app.rehearsal.preparation import PREPARATION_LOCK_KEY
from validation.aws_demo_deployment import RUNTIME_ENVIRONMENT_FIELDS
from validation.aws_review.host_contracts import (
    HostInput,
    HostRefusedError,
    aws_client,
    read_runtime_secret,
)

CONFIG_DIRECTORY = Path("/etc/gs-app-tomidaisai")
POSTGRES_CA_FILE = CONFIG_DIRECTORY / "rds-ca.pem"
RUNTIME_FILE = CONFIG_DIRECTORY / "aws-demo-rehearsal-runtime.env"
RELEASE_FILE = CONFIG_DIRECTORY / "aws-demo-review-release.env"
BACKUP_DIRECTORY = Path("/var/lib/quiz-rehearsal-backups/data")
SESSION_SECRET_DERIVATION_ATTEMPTS = 32


class MasterCredentials(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    username: Literal["quizadmin"]
    password: SecretStr


class RuntimeSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    session_secret: SecretStr


class RedisSecret(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    token: SecretStr


class HostSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    initialization: RehearsalInitializationSecrets
    redis_url: SecretStr
    session_secret: SecretStr
    backup_key: SecretStr


def _derive(seed: SecretStr, purpose: str, *, padded: bool = False) -> SecretStr:
    digest = hmac.digest(seed.get_secret_value().encode("ascii"), purpose.encode("ascii"), "sha256")
    value = base64.urlsafe_b64encode(digest).decode("ascii")
    return SecretStr(value if padded else value.rstrip("="))


def _derive_session_secret(seed: SecretStr) -> SecretStr:
    """既存の有効値を維持し、書式不足の候補だけを決定的に再導出する。"""
    for attempt in range(SESSION_SECRET_DERIVATION_ATTEMPTS):
        purpose = "review-session-v1" if attempt == 0 else f"review-session-v1-retry-{attempt}"
        candidate = _derive(seed, purpose)
        value = candidate.get_secret_value()
        if (
            AWS_DEMO_SESSION_SECRET_PATTERN.fullmatch(value) is not None
            and is_strong_aws_demo_secret(value, minimum_length=43)
            and any(character.islower() for character in value)
            and any(character.isupper() for character in value)
            and any(character.isdigit() for character in value)
        ):
            return candidate
    raise HostRefusedError("review_session_secret_derivation_failed")


def load_secrets(inputs: HostInput) -> HostSecrets:
    """Secrets Manager応答を型へ閉じ込め、用途別の鍵を安定導出する。"""
    client = aws_client("secretsmanager")
    master = MasterCredentials.model_validate_json(
        read_runtime_secret(client, inputs.database_master_secret_arn)
    )
    runtime = RuntimeSeed.model_validate_json(
        read_runtime_secret(client, inputs.runtime_secret_arn)
    )
    redis = RedisSecret.model_validate_json(
        read_runtime_secret(client, inputs.redis_auth_secret_arn)
    )
    if not is_strong_aws_demo_secret(runtime.session_secret.get_secret_value(), minimum_length=43):
        raise HostRefusedError("review_runtime_seed_invalid")
    app_password = _derive(runtime.session_secret, "review-app-database-v1")

    def database_url(username: str, password: SecretStr) -> SecretStr:
        return SecretStr(
            URL.create(
                "postgresql+psycopg",
                username=username,
                password=password.get_secret_value(),
                host=inputs.rds_hostname,
                port=5432,
                database="quiz",
                query={"sslmode": "verify-full"},
            ).render_as_string(hide_password=False)
        )

    return HostSecrets(
        initialization=RehearsalInitializationSecrets(
            admin_database_url=database_url(master.username, master.password),
            app_database_url=database_url(APPLICATION_ROLE, app_password),
            operator_username="rehearsal-operator",
            operator_password=_derive(runtime.session_secret, "review-operator-v1"),
        ),
        redis_url=SecretStr(
            URL.create(
                "rediss",
                username="",
                password=redis.token.get_secret_value(),
                host=inputs.redis_hostname,
                port=6379,
                database="0",
            ).render_as_string(hide_password=False)
        ),
        session_secret=_derive_session_secret(runtime.session_secret),
        backup_key=_derive(runtime.session_secret, "review-backup-v1", padded=True),
    )


def runtime_settings(inputs: HostInput, secrets: HostSecrets, event_id: UUID) -> Settings:
    """既存SettingsのAWS契約を使い、local既定値を確認環境へ持ち込まない。"""
    return Settings(
        _env_file=None,
        app_env="aws-demo",
        app_version=inputs.source_commit,
        database_url=secrets.initialization.app_database_url.get_secret_value(),
        redis_url=secrets.redis_url.get_secret_value(),
        demo_operator_username="managed-in-database",
        demo_operator_password=SecretStr("unused"),
        session_secret=secrets.session_secret,
        enable_demo_auth=True,
        enable_demo_seed=False,
        session_cookie_secure=True,
        operator_session_ttl_minutes=120,
        participant_session_ttl_minutes=120,
        participant_join_retry_minutes=5,
        realtime_allowed_origins=[inputs.public_url],
        api_instance_id="api-rehearsal",
        redis_connect_timeout_seconds=0.25,
        redis_operation_timeout_seconds=0.25,
        postgres_ssl_root_cert=Path("/run/secrets/aws-demo-postgres-ca.pem"),
        aws_demo_expected_event_id=event_id,
        aws_demo_round_scope="rehearsal-six-round",
        logfire_send_to_logfire=False,
        round_backup_storage_mode="filesystem",
        round_backup_directory=Path("/var/lib/quiz-round-backups"),
        round_backup_encryption_key=secrets.backup_key,
    )


def write_private_file(path: Path, body: str) -> None:
    """owner専用の固定出力先だけを書き、symlinkとhardlinkによる秘密漏えいを拒否する。"""
    if not path.is_absolute() or ".." in path.parts:
        raise HostRefusedError("review_private_path_invalid")
    for parent in path.parents:
        status = parent.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid not in {0, os.geteuid()}
            or status.st_mode & 0o022
        ):
            raise HostRefusedError("review_private_parent_unsafe")
    if path.parent.stat().st_uid != os.geteuid():
        raise HostRefusedError("review_private_parent_owner_mismatch")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
        ):
            raise HostRefusedError("review_private_file_unsafe")
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(body)
    finally:
        os.close(descriptor)


def write_runtime_file(settings: Settings, path: Path = RUNTIME_FILE) -> None:
    """既存runtime parserと同じ項目だけを、Compose raw形式で保存する。"""
    values: list[str] = []
    for key, field in RUNTIME_ENVIRONMENT_FIELDS.items():
        value = getattr(settings, field)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        elif isinstance(value, bool):
            value = str(value).lower()
        elif field == "realtime_allowed_origins":
            value = json.dumps([str(origin).rstrip("/") for origin in value], separators=(",", ":"))
        elif field == "app_env":
            value = value.value
        values.append(f"{key}={value}")
    write_private_file(path, "\n".join(values) + "\n")


class DatabaseIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    database_name: Literal["quiz"]
    current_role: Literal["quizadmin"]
    session_role: Literal["quizadmin"]


class DatabasePresence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    present: bool


def reset_dedicated_database(secrets: HostSecrets, postgres_ca: Path = POSTGRES_CA_FILE) -> None:
    """所有検証とservice停止を済ませた専用DBだけを、明示許可済みの空状態へ戻す。"""
    url = make_url(secrets.initialization.admin_database_url.get_secret_value())
    host = validate_remote_dependency_host(
        "database", str(url.render_as_string(hide_password=False))
    )
    with psycopg.connect(
        host=host.hostname,
        hostaddr=host.selected_address,
        port=5432,
        dbname="quiz",
        user="quizadmin",
        password=url.password,
        sslmode="verify-full",
        sslrootcert=str(postgres_ca),
        connect_timeout=5,
        options="-c statement_timeout=30000 -c lock_timeout=5000",
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database() AS database_name, current_user AS current_role, "
                "session_user AS session_role"
            )
            DatabaseIdentity.model_validate(cursor.fetchone())
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (PREPARATION_LOCK_KEY,))
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND usename = %s) AS present",
                (APPLICATION_ROLE,),
            )
            if DatabasePresence.model_validate(cursor.fetchone()).present:
                raise HostRefusedError("review_database_application_still_connected")
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name "
                "NOT IN ('public', 'information_schema') "
                "AND left(schema_name, 3) <> 'pg_') AS present"
            )
            if DatabasePresence.model_validate(cursor.fetchone()).present:
                raise HostRefusedError("review_database_unexpected_schema")
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public AUTHORIZATION quizadmin")
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS present",
                (APPLICATION_ROLE,),
            )
            if DatabasePresence.model_validate(cursor.fetchone()).present:
                cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(APPLICATION_ROLE)))
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(APPLICATION_ROLE)))
