"""既存開催の識別子と設定を保ち、確認済みの進行データだけを初期化する。"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import logfire
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine, create_engine, delete, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel

from quiz_app.config import AppEnvironment, Settings
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    PostgresConnectArgs,
    create_pinned_postgres_engine,
    validate_remote_dependency_host,
)
from quiz_app.domain.errors import DomainInvariantError
from quiz_app.models import (
    AdminSession,
    Answer,
    DisplayNameConsent,
    Event,
    IdempotencyRecord,
    OperationAudit,
    Participant,
    ParticipantSession,
    Question,
    QuizRound,
    RankSnapshot,
    RankSnapshotRow,
    ReadinessReport,
    RoundOptionStat,
    RoundPreparationTarget,
    ScoreLedger,
)
from quiz_app.models.enums import EventState, RankingVisibility, RoundState
from quiz_app.models.participant_ids import (
    PARTICIPANT_ID_POOL_VERSION,
    ParticipantIdPool,
    ParticipantIdSlot,
)
from quiz_app.quiz_configuration import initial_event_configuration, require_event_configuration
from quiz_app.rehearsal.initialization import APPLICATION_ROLE
from quiz_app.rehearsal.preparation import (
    PREPARATION_LOCK_KEY,
    RehearsalPreparationRefusedError,
    _dispose_engine,
    _require_current_schema,
    _require_operator,
)
from validation.aws_review.host_contracts import HostRefusedError
from validation.aws_review.host_data import (
    POSTGRES_CA_FILE,
    DatabaseIdentity,
    DatabasePresence,
    HostSecrets,
)


class ResetDeletedCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    participants: int = Field(ge=0)
    rounds: int = Field(ge=0)
    answers: int = Field(ge=0)
    score_ledgers: int = Field(ge=0)
    operation_audits: int = Field(ge=0)
    idempotency_records: int = Field(ge=0)
    admin_sessions: int = Field(ge=0)


class EventResetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    event_id: UUID
    previous_version: int = Field(gt=0)
    event_version: int = Field(gt=1)
    new_round_id: UUID
    participant_ids_remaining: int = Field(gt=0)
    deleted: ResetDeletedCounts


class _SequencePosition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    last_value: int = Field(ge=1)
    is_called: bool


class _DatabaseTime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    now: datetime


class _SchemaTables(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    names: list[str]


def _require_known_schema(session: Session) -> None:
    row = (
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name "
                "NOT IN ('public', 'information_schema') "
                "AND left(schema_name, 3) <> 'pg_') AS present"
            )
        )
        .mappings()
        .one()
    )
    if DatabasePresence.model_validate(dict(row)).present:
        raise HostRefusedError("review_reset_unexpected_schema")
    tables = _SchemaTables.model_validate(
        {"names": inspect(session.connection()).get_table_names(schema="public")}
    )
    if set(tables.names) != {"alembic_version", *SQLModel.metadata.tables}:
        raise HostRefusedError("review_reset_unexpected_tables")


def _require_database_ready(session: Session) -> None:
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SET LOCAL statement_timeout = '30s'"))
    row = (
        session.execute(
            text(
                "SELECT current_database() AS database_name, current_user AS current_role, "
                "session_user AS session_role"
            )
        )
        .mappings()
        .one()
    )
    DatabaseIdentity.model_validate(dict(row))
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PREPARATION_LOCK_KEY})
    row = (
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND usename = :role) AS present"
            ),
            {"role": APPLICATION_ROLE},
        )
        .mappings()
        .one()
    )
    if DatabasePresence.model_validate(dict(row)).present:
        raise HostRefusedError("review_reset_application_still_connected")
    _require_known_schema(session)
    _require_current_schema(session)
    _require_operator(session)


def _require_available_participant_ids(session: Session, event_id: UUID) -> int:
    pool = session.get(ParticipantIdPool, event_id)
    slots = session.scalar(
        select(func.count())
        .select_from(ParticipantIdSlot)
        .where(ParticipantIdSlot.event_id == event_id)
    )
    if (
        pool is None
        or pool.version != PARTICIPANT_ID_POOL_VERSION
        or pool.capacity <= 0
        or slots != pool.capacity
    ):
        raise HostRefusedError("review_reset_participant_id_pool_invalid")
    statement = sql.SQL("SELECT last_value, is_called FROM public.{}").format(
        sql.Identifier(f"participant_id_{event_id.hex}")
    )
    row = session.connection().exec_driver_sql(statement.as_string()).mappings().one()
    position = _SequencePosition.model_validate(dict(row))
    if position.last_value > pool.capacity or (not position.is_called and position.last_value != 1):
        raise HostRefusedError("review_reset_participant_id_sequence_invalid")
    remaining = pool.capacity - (position.last_value if position.is_called else 0)
    if remaining <= 0:
        raise HostRefusedError("review_reset_participant_ids_exhausted")
    return remaining


def _deleted_counts(session: Session) -> ResetDeletedCounts:
    models: dict[str, type[SQLModel]] = {
        "participants": Participant,
        "rounds": QuizRound,
        "answers": Answer,
        "score_ledgers": ScoreLedger,
        "operation_audits": OperationAudit,
        "idempotency_records": IdempotencyRecord,
        "admin_sessions": AdminSession,
    }
    return ResetDeletedCounts.model_validate(
        {
            name: session.scalar(select(func.count()).select_from(model))
            for name, model in models.items()
        }
    )


def _database_time(session: Session) -> datetime:
    return _DatabaseTime.model_validate(
        dict(session.execute(text("SELECT clock_timestamp() AS now")).mappings().one())
    ).now


def _delete_event_data(session: Session, event_id: UUID) -> None:
    rounds = select(QuizRound.id).where(QuizRound.event_id == event_id)
    participants = select(Participant.id).where(Participant.event_id == event_id)
    snapshots = select(RankSnapshot.id).where(RankSnapshot.event_id == event_id)
    # RESTRICTを解除せず、既存の外部キー依存順に対象開催の子から削除する。
    statements = (
        delete(RankSnapshotRow).where(RankSnapshotRow.snapshot_id.in_(snapshots)),
        delete(RankSnapshot).where(RankSnapshot.event_id == event_id),
        delete(ScoreLedger).where(ScoreLedger.round_id.in_(rounds)),
        delete(Answer).where(Answer.round_id.in_(rounds)),
        delete(ReadinessReport).where(ReadinessReport.round_id.in_(rounds)),
        delete(RoundPreparationTarget).where(RoundPreparationTarget.round_id.in_(rounds)),
        delete(RoundOptionStat).where(RoundOptionStat.round_id.in_(rounds)),
        delete(OperationAudit).where(OperationAudit.event_id == event_id),
        delete(QuizRound).where(QuizRound.event_id == event_id),
        delete(ParticipantSession).where(ParticipantSession.participant_id.in_(participants)),
        delete(DisplayNameConsent).where(DisplayNameConsent.participant_id.in_(participants)),
        delete(Participant).where(Participant.event_id == event_id),
        # この2表は開催FKを持たないため、単一開催と専用DBの確認後だけ全件を失効させる。
        delete(IdempotencyRecord),
        delete(AdminSession),
    )
    for statement in statements:
        session.execute(statement.execution_options(synchronize_session=False))


def _reset_in_transaction(
    session: Session,
    event_id: UUID,
    expected_version: int,
    reset_id: UUID,
    before_delete: Callable[[Session], None],
) -> EventResetResult:
    _require_database_ready(session)
    events = session.scalars(select(Event).with_for_update()).all()
    if len(events) != 1 or events[0].id != event_id:
        raise HostRefusedError("review_reset_single_event_mismatch")
    event = events[0]
    if event.version != expected_version:
        raise HostRefusedError("review_reset_event_version_mismatch")
    configuration = require_event_configuration(session, event)
    remaining = _require_available_participant_ids(session, event_id)
    question = session.scalar(
        select(Question)
        .where(Question.event_id == event_id)
        .order_by(Question.display_order, Question.id)
        .limit(1)
    )
    if question is None:
        raise HostRefusedError("review_reset_questions_missing")
    result = EventResetResult(
        event_id=event_id,
        previous_version=event.version,
        event_version=event.version + 1,
        new_round_id=uuid4(),
        participant_ids_remaining=remaining,
        deleted=_deleted_counts(session),
    )
    before_state = event.state.value
    # 同じtransactionでarchiveの保存・復号照合が完了するまで、削除も状態変更もしない。
    before_delete(session)
    _delete_event_data(session, event_id)
    timestamp = _database_time(session)
    event.state = EventState.LOBBY_OPEN
    event.ranking_visibility = RankingVisibility.HIDDEN_ALL
    event.version = result.event_version
    event.updated_at = timestamp
    initial = initial_event_configuration(event_id, configuration.settings)
    configuration.record.settings_json = initial.settings_json
    for field in (
        "distribution_visible",
        "personal_ranking_visible",
        "overall_ranking_visible",
        "last_applied_round",
    ):
        setattr(configuration.record, field, getattr(initial, field))
    session.add(
        QuizRound(
            id=result.new_round_id,
            event_id=event_id,
            question_id=question.id,
            version=1,
            state=RoundState.PREPARING_ASSETS,
            created_at=timestamp,
        )
    )
    session.add(
        OperationAudit(
            event_id=event_id,
            operation="reset_event",
            target_type="event",
            target_id=str(event_id),
            before_state=before_state,
            after_state=EventState.LOBBY_OPEN.value,
            reason=result.model_dump_json(),
            outcome="succeeded",
            idempotency_key=reset_id,
            occurred_at=timestamp,
        )
    )
    session.flush()
    return result


def _require_reset_settings(
    settings: Settings, event_id: UUID, expected_version: int, postgres_ca: Path
) -> None:
    if (
        settings.app_env is not AppEnvironment.AWS_DEMO
        or settings.aws_demo_round_scope != "rehearsal-six-round"
        or settings.aws_demo_expected_event_id != event_id
        or settings.enable_demo_seed
        or not postgres_ca.is_absolute()
        or expected_version < 1
    ):
        raise HostRefusedError("review_reset_runtime_target_invalid")


def _create_reset_engine(database_url: str, postgres_ca: Path) -> Engine:
    host = validate_remote_dependency_host("database", database_url)

    def host_engine(
        url: str, *, pool_pre_ping: bool, pool_recycle: int, connect_args: PostgresConnectArgs
    ) -> Engine:
        return create_engine(
            url,
            pool_pre_ping=pool_pre_ping,
            pool_recycle=pool_recycle,
            connect_args={
                **connect_args,
                "sslrootcert": str(postgres_ca),
                "options": "-c statement_timeout=30000 -c lock_timeout=5000",
            },
        )

    return create_pinned_postgres_engine(database_url, host, engine_factory=host_engine)


def inspect_reset_event(
    settings: Settings,
    event_id: UUID,
    expected_version: int,
    *,
    postgres_ca: Path = POSTGRES_CA_FILE,
) -> None:
    """停止前にapplication roleのread-only transactionで対象開催を照合する。"""
    _require_reset_settings(settings, event_id, expected_version, postgres_ca)
    engine: Engine | None = None
    try:
        engine = _create_reset_engine(settings.database_url, postgres_ca)
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            _require_known_schema(session)
            _require_current_schema(session)
            events = session.scalars(select(Event)).all()
            if len(events) != 1 or events[0].id != event_id:
                raise HostRefusedError("review_reset_single_event_mismatch")
            if events[0].version != expected_version:
                raise HostRefusedError("review_reset_event_version_mismatch")
            require_event_configuration(session, events[0])
    except (
        SQLAlchemyError,
        ValidationError,
        DomainInvariantError,
        RehearsalPreparationRefusedError,
        DependencyHostValidationError,
    ):
        try:
            raise HostRefusedError("review_reset_inspection_failed") from None
        except HostRefusedError:
            logfire.exception("review event reset inspection failed")
            raise
    finally:
        if engine is not None:
            _dispose_engine(engine, event_committed=False)


def reset_event_data(
    settings: Settings,
    secrets: HostSecrets,
    event_id: UUID,
    expected_version: int,
    *,
    reset_id: UUID,
    before_delete: Callable[[Session], None],
    postgres_ca: Path = POSTGRES_CA_FILE,
) -> EventResetResult:
    """停止済み専用環境の確認・archive・初期化を一transactionで確定する。"""
    _require_reset_settings(settings, event_id, expected_version, postgres_ca)
    if settings.database_url != secrets.initialization.app_database_url.get_secret_value():
        raise HostRefusedError("review_reset_runtime_target_invalid")
    admin_url = secrets.initialization.admin_database_url.get_secret_value()
    target = make_url(admin_url)
    if target.username != "quizadmin" or target.database != "quiz" or target.port != 5432:
        raise HostRefusedError("review_reset_database_target_invalid")
    engine: Engine | None = None
    committed = False
    try:
        engine = _create_reset_engine(admin_url, postgres_ca)
        with Session(engine) as session, session.begin():
            result = _reset_in_transaction(
                session, event_id, expected_version, reset_id, before_delete
            )
        committed = True
    except (
        SQLAlchemyError,
        ValidationError,
        DomainInvariantError,
        RehearsalPreparationRefusedError,
        DependencyHostValidationError,
    ):
        # driver/validationの本文は接続先の秘密や正答を含み得るため外へ連鎖させない。
        try:
            raise HostRefusedError("review_reset_database_failed") from None
        except HostRefusedError:
            logfire.exception("review event reset database failed")
            raise
    finally:
        if engine is not None:
            _dispose_engine(engine, event_committed=committed)
    logfire.info(
        "review event reset committed", event_id=event_id, event_version=result.event_version
    )
    return result
