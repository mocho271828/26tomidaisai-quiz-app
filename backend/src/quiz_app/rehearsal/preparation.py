"""人が準備した専用DBへ、選択済み問題の開催を一度だけ登録する。"""

from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import logfire
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, SQLModel

from quiz_app.config import (
    AWS_DEMO_OPERATOR_USERNAME_SENTINEL,
    EVENT_SLUG_PATTERN,
    LOCAL_DEMO_EVENT_ID,
    AppEnvironment,
    Settings,
)
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    PostgresConnectArgs,
    create_pinned_postgres_engine,
    validate_remote_dependency_host,
)
from quiz_app.models import Event, Question, QuestionOption, QuizRound
from quiz_app.models.enums import EventState, RoundState
from quiz_app.operator.auth_service import is_current_operator_password_hash
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)
from quiz_app.rehearsal.content import RehearsalContent
from quiz_app.schema_revision import SCHEMA_REVISION

PREPARED_TABLES = frozenset({"scoring_policies", "display_name_policies", "admin_accounts"})
# 他用途のbootstrapとも同じlockを使い、単一開催の確認と登録を競合させない。
PREPARATION_LOCK_KEY = int.from_bytes(
    sha256(b"single-event-load-test-bootstrap").digest()[:8], "big", signed=True
)


class RehearsalPreparationRefusedError(RuntimeError):
    """既存DBを変更せず準備を拒否したことを固定codeで表す。"""


class RehearsalEventInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    event_id: UUID
    title: str = Field(min_length=1, max_length=160)
    slug: str = Field(pattern=EVENT_SLUG_PATTERN)
    scoring_policy_version: str = Field(min_length=1, max_length=64)
    display_name_policy_version: str = Field(min_length=1, max_length=64)

    @field_validator("event_id")
    @classmethod
    def require_fresh_identity(cls, value: UUID) -> UUID:
        if value == LOCAL_DEMO_EVENT_ID or value.version != 4:
            raise ValueError("rehearsal_requires_new_uuid4_event_id")
        return value


class RehearsalPreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    schema_revision: str = SCHEMA_REVISION
    created_events: Literal[1] = 1
    created_questions: int = Field(ge=3)
    created_options: int = Field(ge=12)
    created_rounds: Literal[1] = 1


class _DatabaseRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _SchemaRevisionRow(_DatabaseRow):
    version_num: str


class _PolicyRow(_DatabaseRow):
    version: str


class _ScoringRow(_PolicyRow):
    is_approved: bool
    base_points: int
    initial_speed_bonus: int
    speed_bucket_ms: int
    speed_decrement_per_bucket: int
    standard_duration_ms: int
    timing_basis: Literal["server_received_v1", "client_monotonic_v1"]
    arrival_grace_ms: int
    clock_tolerance_ms: int
    client_ahead_tolerance_ms: int


class _OperatorRow(_DatabaseRow):
    username: str = Field(min_length=1, max_length=80)
    password_hash: str = Field(min_length=1, max_length=256, repr=False)
    role: Literal["Operator", "Supervisor"]
    is_demo: bool
    is_revoked: bool


class _PresenceRow(_DatabaseRow):
    exists: bool


def validate_rehearsal_settings(settings: Settings, event: RehearsalEventInput) -> None:
    """検証済みAWS設定が今回の開催へ固定されていることを確認する。"""
    if settings.app_env is not AppEnvironment.AWS_DEMO:
        raise RehearsalPreparationRefusedError("rehearsal_requires_aws_demo")
    if settings.aws_demo_round_scope != "rehearsal-six-round":
        raise RehearsalPreparationRefusedError("rehearsal_requires_six_round_scope")
    if settings.enable_demo_seed:
        raise RehearsalPreparationRefusedError("rehearsal_requires_demo_seed_disabled")
    if settings.aws_demo_expected_event_id != event.event_id:
        raise RehearsalPreparationRefusedError("rehearsal_event_id_mismatch")


def _require_current_schema(session: Session) -> None:
    required_tables = {"alembic_version", *SQLModel.metadata.tables}
    if not required_tables.issubset(inspect(session.connection()).get_table_names()):
        raise RehearsalPreparationRefusedError("rehearsal_schema_missing")
    rows = session.execute(text("SELECT version_num FROM alembic_version")).mappings().all()
    revisions = [_SchemaRevisionRow.model_validate(dict(row)).version_num for row in rows]
    if revisions != [SCHEMA_REVISION]:
        raise RehearsalPreparationRefusedError("rehearsal_schema_revision_mismatch")


def _require_empty_event_data(session: Session) -> None:
    # policyとDB管理account以外は空を必須とし、孤立した回答・session等も再利用しない。
    for table in SQLModel.metadata.sorted_tables:
        if table.name in PREPARED_TABLES:
            continue
        presence = _PresenceRow.model_validate(
            {"exists": session.scalar(select(select(table.columns[0]).exists()))}
        )
        if presence.exists:
            raise RehearsalPreparationRefusedError("rehearsal_database_not_empty")


def _require_policies(session: Session, event: RehearsalEventInput) -> None:
    scoring_rows = (
        session.execute(
            text(
                "SELECT version, is_approved, base_points, initial_speed_bonus, speed_bucket_ms, "
                "speed_decrement_per_bucket, standard_duration_ms, timing_basis, "
                "arrival_grace_ms, clock_tolerance_ms, client_ahead_tolerance_ms "
                "FROM scoring_policies WHERE version = :version"
            ),
            {"version": event.scoring_policy_version},
        )
        .mappings()
        .all()
    )
    scoring = [_ScoringRow.model_validate(dict(row)) for row in scoring_rows]
    expected_policy = configured_scoring_policy(get_quiz_settings()).model_dump(
        exclude={"created_at"}
    )
    if (
        len(scoring) != 1
        or scoring[0].version != event.scoring_policy_version
        or scoring[0].model_dump() != expected_policy
    ):
        raise RehearsalPreparationRefusedError("rehearsal_approved_scoring_policy_required")
    display_rows = (
        session.execute(
            text("SELECT version FROM display_name_policies WHERE version = :version"),
            {"version": event.display_name_policy_version},
        )
        .mappings()
        .all()
    )
    display = [_PolicyRow.model_validate(dict(row)) for row in display_rows]
    if len(display) != 1 or display[0].version != event.display_name_policy_version:
        raise RehearsalPreparationRefusedError("rehearsal_display_name_policy_required")


def _require_operator(session: Session) -> None:
    rows = (
        session.execute(
            text("SELECT username, password_hash, role, is_demo, is_revoked FROM admin_accounts")
        )
        .mappings()
        .all()
    )
    operators = [_OperatorRow.model_validate(dict(row)) for row in rows]
    demo_operators = [operator for operator in operators if operator.is_demo]
    local_names = {
        AWS_DEMO_OPERATOR_USERNAME_SENTINEL,
        "default-operator",
        "demo-operator",
        "review-operator",
    }
    has_local_identity = any(
        operator.username.casefold() in local_names
        or operator.username.casefold().startswith(("local-", "local_"))
        or operator.username.casefold().endswith(("@localhost", ".localhost"))
        for operator in operators
    )
    if (
        has_local_identity
        or len(demo_operators) != 1
        or demo_operators[0].is_revoked
        or demo_operators[0].role != "Operator"
        or not is_current_operator_password_hash(demo_operators[0].password_hash)
    ):
        raise RehearsalPreparationRefusedError("rehearsal_database_operator_required")


def _insert_records(
    session: Session, event: RehearsalEventInput, content: RehearsalContent
) -> None:
    session.add(
        Event(
            id=event.event_id,
            slug=event.slug,
            title=event.title,
            state=EventState.LOBBY_OPEN,
            scoring_policy_version=event.scoring_policy_version,
            display_name_policy_version=event.display_name_policy_version,
        )
    )
    session.flush()
    session.add(initial_event_configuration(event.event_id, get_quiz_settings()))
    question_ids = [uuid4() for _ in content.selected_questions]
    for order, (question_id, question) in enumerate(
        zip(question_ids, content.selected_questions, strict=True), start=1
    ):
        session.add(
            Question(
                id=question_id,
                event_id=event.event_id,
                version=order,
                display_order=order,
                prompt=question.display_prompt,
                explanation=question.explanation,
            )
        )
    session.flush()
    for question_id, question in zip(question_ids, content.selected_questions, strict=True):
        for order, option in enumerate(question.options):
            session.add(
                QuestionOption(
                    id=uuid4(),
                    question_id=question_id,
                    label=option.label,
                    content=option.content,
                    display_order=order,
                    is_correct=question.correct_label == option.label,
                )
            )
    session.flush()
    session.add(
        QuizRound(
            id=uuid4(),
            event_id=event.event_id,
            question_id=question_ids[0],
            version=1,
            state=RoundState.PREPARING_ASSETS,
        )
    )
    session.flush()


def _insert_rehearsal_event(
    engine: Engine, event: RehearsalEventInput, content: RehearsalContent
) -> RehearsalPreparationResult:
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '30s'"))
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": PREPARATION_LOCK_KEY}
        )
        _require_current_schema(session)
        _require_empty_event_data(session)
        _require_policies(session, event)
        _require_operator(session)
        _insert_records(session, event, content)
    return RehearsalPreparationResult(
        event_id=event.event_id,
        created_questions=len(content.selected_questions),
        created_options=sum(len(question.options) for question in content.selected_questions),
    )


def _dispose_engine(engine: Engine, *, event_committed: bool) -> None:
    try:
        engine.dispose()
    except (SQLAlchemyError, OSError):
        code = (
            "rehearsal_database_cleanup_failed_after_commit"
            if event_committed
            else "rehearsal_database_cleanup_failed"
        )
        try:
            raise RehearsalPreparationRefusedError(code) from None
        except RehearsalPreparationRefusedError:
            logfire.exception("rehearsal preparation connection cleanup failed")
            # 元の処理が失敗した場合は、その固定codeをcleanup例外で置き換えない。
            if event_committed:
                raise


def prepare_rehearsal_event(
    settings: Settings,
    event: RehearsalEventInput,
    content: RehearsalContent,
    *,
    postgres_ca_file: Path,
) -> RehearsalPreparationResult:
    """schema適用や既存data変更をせず、人が用意した専用DBへ1開催を登録する。"""
    validate_rehearsal_settings(settings, event)
    if not postgres_ca_file.is_absolute():
        raise RehearsalPreparationRefusedError("rehearsal_postgres_ca_path_not_absolute")

    def create_host_engine(
        database_url: str,
        *,
        pool_pre_ping: bool,
        pool_recycle: int,
        connect_args: PostgresConnectArgs,
    ) -> Engine:
        # CLIがchecksum検証したhost側CAを指定し、container pathやambient envに依存しない。
        return create_engine(
            database_url,
            pool_pre_ping=pool_pre_ping,
            pool_recycle=pool_recycle,
            connect_args={**connect_args, "sslrootcert": str(postgres_ca_file)},
        )

    engine: Engine | None = None
    event_committed = False
    logfire.info(
        "rehearsal preparation started", selected_questions=len(content.selected_questions)
    )
    try:
        host = validate_remote_dependency_host("database", settings.database_url)
        engine = create_pinned_postgres_engine(
            settings.database_url, host, engine_factory=create_host_engine
        )
        result = _insert_rehearsal_event(engine, event, content)
        event_committed = True
    except DependencyHostValidationError:
        raise RehearsalPreparationRefusedError("rehearsal_database_host_invalid") from None
    except (SQLAlchemyError, ValidationError) as error:
        code = (
            "rehearsal_concurrent_identity_collision"
            if isinstance(error, IntegrityError)
            else "rehearsal_database_failed"
        )
        # driver例外は接続URL・binding・正答を含み得るため、固定例外のtraceだけを残す。
        try:
            raise RehearsalPreparationRefusedError(code) from None
        except RehearsalPreparationRefusedError:
            logfire.exception("rehearsal preparation database failed")
            raise
    except RehearsalPreparationRefusedError:
        logfire.exception("rehearsal preparation refused")
        raise
    finally:
        if engine is not None:
            _dispose_engine(engine, event_committed=event_committed)
    logfire.info(
        "rehearsal preparation completed",
        selected_questions=result.created_questions,
        initial_rounds=result.created_rounds,
    )
    return result
