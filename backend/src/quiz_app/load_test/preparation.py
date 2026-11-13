"""人がschemaを適用した空の試験DBへ、run固有データだけを一度作成する。"""

from uuid import UUID, uuid5

import logfire
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, SQLModel

from quiz_app.config import Settings
from quiz_app.dependency_hosts import create_pinned_postgres_engine
from quiz_app.load_test.bootstrap import (
    LOCK_TIMEOUT,
    STATEMENT_TIMEOUT,
    Clock,
    LoadTestBootstrapRefusedError,
    LoadTestBootstrapResult,
    _advisory_lock_key,
    _insert_manifest_records,
    _require_current_schema,
    _require_future_schedule,
    _require_remote_database_host,
    _utc_now,
    validate_bootstrap_settings,
    validate_manifest_quiz_configuration,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.models import AdminAccount, DisplayNamePolicy, ScoringPolicy
from quiz_app.models.enums import AdminRole
from quiz_app.operator.auth_service import create_operator_password_hasher

CREDENTIAL_POLICY_VERSION = "argon2id-v1"


class RunOperatorCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    password: SecretStr

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if not 32 <= len(password) <= 128 or not password.isascii():
            raise ValueError("load_test_operator_password_invalid")
        if not all(
            (
                any(c.isupper() for c in password),
                any(c.islower() for c in password),
                any(c.isdigit() for c in password),
            )
        ):
            raise ValueError("load_test_operator_password_invalid")
        return value


def run_operator_username(run_id: UUID) -> str:
    """実利用者名を使わない固定の試験運営名を返す。"""
    return f"loadtest-{run_id.hex}"


def _insert_fresh_run(
    engine: Engine,
    manifest: LoadTestManifest,
    credential: RunOperatorCredential,
) -> LoadTestBootstrapResult:
    validate_manifest_quiz_configuration(manifest)
    # Argon2の計算はtransaction前に済ませ、DB lock保持時間を増やさない。
    password_hash = create_operator_password_hasher().hash(credential.password.get_secret_value())
    with Session(engine) as session, session.begin():
        session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key(manifest)},
        )
        _require_current_schema(session)
        # Event以外の孤立データも拒否する。schema・既存データの修復や再利用はしない。
        for table in SQLModel.metadata.sorted_tables:
            if session.scalar(select(table.columns[0]).limit(1)) is not None:
                raise LoadTestBootstrapRefusedError("load_test_preparation_database_not_empty")
        scoring = manifest.event.scoring_policy
        display = manifest.event.display_name_policy
        session.add(ScoringPolicy(**scoring.model_dump()))
        session.add(DisplayNamePolicy(**display.model_dump()))
        session.add(
            AdminAccount(
                id=uuid5(manifest.run_id, "operator"),
                username=run_operator_username(manifest.run_id),
                password_hash=password_hash,
                role=AdminRole.OPERATOR,
                password_policy_version=CREDENTIAL_POLICY_VERSION,
                is_demo=True,
                is_revoked=False,
            )
        )
        session.flush()
        _insert_manifest_records(session, manifest)
    return LoadTestBootstrapResult(
        run_id=str(manifest.run_id),
        event_id=str(manifest.event.id),
        schema_revision=manifest.schema_revision,
        created_events=1,
        created_questions=10,
        created_options=40,
        created_rounds=1,
    )


def prepare_fresh_load_test_run(
    settings: Settings,
    manifest: LoadTestManifest,
    credential: RunOperatorCredential,
    *,
    clock: Clock = _utc_now,
) -> LoadTestBootstrapResult:
    """schema適用は行わず、空DBの確認と試験データINSERTを単一transactionで行う。"""
    validate_bootstrap_settings(settings, manifest)
    _require_future_schedule(manifest, clock)
    host = _require_remote_database_host(settings)
    engine = create_pinned_postgres_engine(settings.database_url, host)
    logfire.info("load test preparation started", run_id=str(manifest.run_id))
    try:
        result = _insert_fresh_run(engine, manifest, credential)
    except IntegrityError:
        logfire.error("load test preparation refused", code="concurrent_identity_collision")
        raise LoadTestBootstrapRefusedError(
            "load_test_bootstrap_concurrent_identity_collision"
        ) from None
    except SQLAlchemyError:
        # DB driver例外はURLやbindingを含み得るため、固定例外のtraceへ置き換える。
        try:
            raise LoadTestBootstrapRefusedError("load_test_preparation_database_failed") from None
        except LoadTestBootstrapRefusedError:
            logfire.exception("load test preparation database failed")
            raise
    finally:
        engine.dispose()
    logfire.info("load test preparation completed", run_id=str(manifest.run_id), questions=10)
    return result
