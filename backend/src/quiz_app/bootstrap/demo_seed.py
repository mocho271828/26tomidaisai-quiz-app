from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

import logfire
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlmodel import Session, SQLModel

from quiz_app.bootstrap.demo_content import (
    DEMO_QUESTION_ID,
    DEMO_QUESTION_IDS,
    DEMO_QUESTION_SEEDS,
)
from quiz_app.config import AppEnvironment, Settings, get_settings
from quiz_app.db import get_engine
from quiz_app.models import (
    AdminAccount,
    DisplayNamePolicy,
    Event,
    EventQuizConfiguration,
    Question,
    QuestionOption,
    QuizRound,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    AdminRole,
    EventState,
    RankingVisibility,
    RoundResultPublication,
    RoundState,
)
from quiz_app.operator.auth_service import OperatorAuthService
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)
from quiz_app.schema_revision import SCHEMA_REVISION

DEMO_EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
DEMO_ROUND_ID = UUID("60000000-0000-4000-8000-000000000007")
DEMO_OPERATOR_ID = UUID("60000000-0000-4000-8000-000000000008")

DEMO_EVENT_SLUG = "demo-2026"
DEMO_SCORING_POLICY_VERSION = get_quiz_settings().scoring.version
DEMO_DISPLAY_NAME_POLICY_VERSION = "demo-display-name-v1"
DEMO_CREDENTIAL_POLICY_VERSION = "argon2id-v1"
DEMO_SEED_LOCK_KEY = 6_000_000_000_000_001
DEMO_DATABASE_HOST = "postgres"
DEMO_DATABASE_PORT = 5432
DEMO_DATABASE_NAME = "quiz_demo"
DEMO_DATABASE_USERNAME = "quiz"
REQUIRED_SEED_TABLES = frozenset(
    {
        "admin_accounts",
        "alembic_version",
        "display_name_policies",
        "events",
        "event_quiz_configurations",
        "question_options",
        "questions",
        "quiz_rounds",
        "scoring_policies",
    }
)
ModelType = TypeVar("ModelType", bound=SQLModel)


class DemoSeedRefusedError(RuntimeError):
    """安全条件または既存seedの整合性を満たさない場合の拒否。"""


@dataclass(frozen=True, slots=True)
class DemoSeedResult:
    created_count: int
    existing_count: int


def seed_demo_data(engine: Engine, settings: Settings) -> DemoSeedResult:
    """migration済みlocal PostgreSQLへ固定demo dataを冪等に投入する。"""

    _validate_seed_configuration(settings)
    _validate_demo_database_target(engine.url)
    _validate_current_schema(engine)
    auth_service = OperatorAuthService(engine, settings)

    with Session(engine) as session, session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": DEMO_SEED_LOCK_KEY},
        )
        return _reconcile_demo_records(
            session,
            settings,
            lambda: auth_service.hash_password(settings.demo_operator_password),
            lambda password_hash: auth_service.password_matches(
                password_hash,
                settings.demo_operator_password,
            ),
        )


def _validate_seed_configuration(settings: Settings) -> None:
    if settings.app_env is not AppEnvironment.LOCAL_DEMO:
        raise DemoSeedRefusedError("demo_seed_requires_local_demo_environment")
    if not settings.enable_demo_seed:
        raise DemoSeedRefusedError("demo_seed_is_disabled")
    if not settings.enable_demo_auth:
        raise DemoSeedRefusedError("demo_seed_requires_demo_auth")

    _validate_demo_database_target(settings.database_url)


def _validate_demo_database_target(database_url_value: str | URL) -> None:
    try:
        database_url = make_url(database_url_value)
    except ArgumentError as error:
        raise DemoSeedRefusedError("demo_seed_database_url_invalid") from error
    if database_url.get_backend_name() != "postgresql":
        raise DemoSeedRefusedError("demo_seed_requires_postgresql")
    if database_url.host != DEMO_DATABASE_HOST:
        raise DemoSeedRefusedError("demo_seed_requires_compose_database_host")
    if database_url.port != DEMO_DATABASE_PORT:
        raise DemoSeedRefusedError("demo_seed_requires_compose_database_port")
    if database_url.database != DEMO_DATABASE_NAME:
        raise DemoSeedRefusedError("demo_seed_requires_dedicated_database_name")
    if database_url.username != DEMO_DATABASE_USERNAME:
        raise DemoSeedRefusedError("demo_seed_requires_dedicated_database_user")
    if database_url.query:
        raise DemoSeedRefusedError("demo_seed_database_options_not_allowed")


def _validate_current_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        revisions: list[str] = []
        if "alembic_version" in table_names:
            revisions = list(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
    _require_current_schema(table_names, revisions)


def _require_current_schema(table_names: Collection[str], revisions: Collection[str]) -> None:
    missing_tables = REQUIRED_SEED_TABLES.difference(table_names)
    if missing_tables:
        raise DemoSeedRefusedError("demo_seed_schema_missing")
    if len(revisions) != 1 or SCHEMA_REVISION not in revisions:
        raise DemoSeedRefusedError("demo_seed_schema_revision_mismatch")


def _reconcile_demo_records(
    session: Session,
    settings: Settings,
    password_hash_factory: Callable[[], str],
    password_hash_matches: Callable[[str], bool],
) -> DemoSeedResult:
    created_count = 0
    existing_count = 0

    quiz_settings = get_quiz_settings()
    scoring_policy = configured_scoring_policy(quiz_settings)
    created_count, existing_count = _record_outcome(
        _insert_if_missing(
            session,
            ScoringPolicy,
            scoring_policy.version,
            scoring_policy,
            scoring_policy.model_dump(exclude={"created_at", "version"}),
        ),
        created_count,
        existing_count,
    )
    display_name_policy = DisplayNamePolicy(
        version=DEMO_DISPLAY_NAME_POLICY_VERSION,
        notice_version="demo-notice-v1",
        max_code_points=20,
        reject_patterns_json="[]",
    )
    created_count, existing_count = _record_outcome(
        _insert_if_missing(
            session,
            DisplayNamePolicy,
            display_name_policy.version,
            display_name_policy,
            {
                "notice_version": "demo-notice-v1",
                "max_code_points": 20,
                "reject_patterns_json": "[]",
            },
        ),
        created_count,
        existing_count,
    )
    session.flush()

    event = Event(
        id=DEMO_EVENT_ID,
        slug=DEMO_EVENT_SLUG,
        title="[DEMO] 2026 富大祭クイズ UI確認",
        state=EventState.LOBBY_OPEN,
        ranking_visibility=RankingVisibility.HIDDEN_ALL,
        scoring_policy_version=quiz_settings.scoring.version,
        display_name_policy_version=DEMO_DISPLAY_NAME_POLICY_VERSION,
        version=1,
    )
    is_new_event = _insert_seed_event(session, event)
    created_count, existing_count = _record_outcome(
        is_new_event,
        created_count,
        existing_count,
    )
    session.flush()

    configuration = initial_event_configuration(event.id, quiz_settings)
    if not is_new_event and session.get(EventQuizConfiguration, event.id) is None:
        raise DemoSeedRefusedError("demo_seed_existing_event_configuration_missing")
    created_count, existing_count = _record_outcome(
        _insert_if_missing(
            session,
            EventQuizConfiguration,
            event.id,
            configuration,
            {"settings_json": configuration.settings_json},
        ),
        created_count,
        existing_count,
    )

    for question_seed in DEMO_QUESTION_SEEDS:
        question = Question(
            id=question_seed.id,
            event_id=DEMO_EVENT_ID,
            version=question_seed.version,
            prompt=question_seed.prompt,
            explanation=question_seed.explanation,
            display_order=question_seed.version,
        )
        created_count, existing_count = _record_outcome(
            _insert_seed_question(session, question),
            created_count,
            existing_count,
        )
        session.flush()

        for display_order, option_seed in enumerate(question_seed.options):
            option = QuestionOption(
                id=option_seed.id,
                question_id=question_seed.id,
                label=option_seed.label,
                content=option_seed.content,
                display_order=display_order,
                is_correct=option_seed.is_correct,
            )
            created_count, existing_count = _record_outcome(
                _insert_seed_option(session, option),
                created_count,
                existing_count,
            )
        session.flush()

    round_ = QuizRound(
        id=DEMO_ROUND_ID,
        event_id=DEMO_EVENT_ID,
        question_id=DEMO_QUESTION_ID,
        version=1,
        state=RoundState.PREPARING_ASSETS,
        result_publication=RoundResultPublication.WITHHELD,
    )
    created_count, existing_count = _record_outcome(
        _insert_seed_round(session, round_),
        created_count,
        existing_count,
    )

    account = session.get(AdminAccount, DEMO_OPERATOR_ID)
    if account is None:
        conflicting_account = session.scalar(
            select(AdminAccount).where(AdminAccount.username == settings.demo_operator_username)
        )
        if conflicting_account is not None:
            raise DemoSeedRefusedError("demo_seed_operator_identity_conflict")
        account = AdminAccount(
            id=DEMO_OPERATOR_ID,
            username=settings.demo_operator_username,
            password_hash=password_hash_factory(),
            role=AdminRole.OPERATOR,
            password_policy_version=DEMO_CREDENTIAL_POLICY_VERSION,
            is_demo=True,
            is_revoked=False,
        )
        session.add(account)
        created_count += 1
    else:
        _require_fields(
            account,
            "operator",
            {
                "username": settings.demo_operator_username,
                "role": AdminRole.OPERATOR,
                "password_policy_version": DEMO_CREDENTIAL_POLICY_VERSION,
                "is_demo": True,
            },
        )
        if not account.password_hash.startswith("$argon2id$") or not password_hash_matches(
            account.password_hash
        ):
            raise DemoSeedRefusedError("demo_seed_operator_credentials_mismatch")
        existing_count += 1
    session.flush()

    return DemoSeedResult(created_count=created_count, existing_count=existing_count)


def _insert_seed_event(session: Session, event: Event) -> bool:
    conflicting_event = session.scalar(select(Event).where(Event.id != event.id).limit(1))
    if conflicting_event is not None:
        raise DemoSeedRefusedError("demo_seed_requires_single_event")

    existing = session.get(Event, event.id)
    if existing is None:
        if session.scalar(select(Event).where(Event.slug == event.slug)) is not None:
            raise DemoSeedRefusedError("demo_seed_event_slug_conflict")
        session.add(event)
        return True
    _require_fields(
        existing,
        "event",
        {
            "slug": event.slug,
            "title": event.title,
            "scoring_policy_version": event.scoring_policy_version,
            "display_name_policy_version": event.display_name_policy_version,
        },
    )
    return False


def _insert_seed_question(session: Session, question: Question) -> bool:
    existing = session.get(Question, question.id)
    if existing is None:
        conflict = session.scalar(
            select(Question).where(
                Question.event_id == question.event_id,
                Question.version == question.version,
            )
        )
        if conflict is not None:
            raise DemoSeedRefusedError("demo_seed_question_version_conflict")
        session.add(question)
        return True
    _require_fields(
        existing,
        "question",
        {
            "event_id": question.event_id,
            "version": question.version,
            "prompt": question.prompt,
            "explanation": question.explanation,
            "display_order": question.display_order,
        },
    )
    return False


def _insert_seed_option(session: Session, option: QuestionOption) -> bool:
    existing = session.get(QuestionOption, option.id)
    if existing is None:
        conflict = session.scalar(
            select(QuestionOption).where(
                QuestionOption.question_id == option.question_id,
                (QuestionOption.label == option.label)
                | (QuestionOption.display_order == option.display_order),
            )
        )
        if conflict is not None:
            raise DemoSeedRefusedError("demo_seed_question_option_conflict")
        session.add(option)
        return True
    _require_fields(
        existing,
        "question_option",
        {
            "question_id": option.question_id,
            "label": option.label,
            "content": option.content,
            "display_order": option.display_order,
            "is_correct": option.is_correct,
        },
    )
    return False


def _insert_seed_round(session: Session, round_: QuizRound) -> bool:
    existing = session.get(QuizRound, round_.id)
    if existing is None:
        conflict = session.scalar(select(QuizRound).where(QuizRound.event_id == round_.event_id))
        if conflict is not None:
            raise DemoSeedRefusedError("demo_seed_round_conflict")
        session.add(round_)
        return True
    _require_fields(
        existing,
        "round",
        {
            "event_id": round_.event_id,
        },
    )
    if existing.question_id not in DEMO_QUESTION_IDS:
        raise DemoSeedRefusedError("demo_seed_round_mismatch:question_id")
    return False


def _insert_if_missing(
    session: Session,
    model_type: type[ModelType],
    primary_key: object,
    candidate: ModelType,
    expected_fields: Mapping[str, object],
) -> bool:
    existing = session.get(model_type, primary_key)
    if existing is None:
        session.add(candidate)
        return True
    _require_fields(existing, model_type.__name__, expected_fields)
    return False


def _require_fields(
    existing: object,
    record_name: str,
    expected_fields: Mapping[str, object],
) -> None:
    mismatched_fields = [
        field_name
        for field_name, expected_value in expected_fields.items()
        if getattr(existing, field_name) != expected_value
    ]
    if mismatched_fields:
        fields = ",".join(sorted(mismatched_fields))
        raise DemoSeedRefusedError(f"demo_seed_{record_name}_mismatch:{fields}")


def _record_outcome(
    is_created: bool,
    created_count: int,
    existing_count: int,
) -> tuple[int, int]:
    if is_created:
        return created_count + 1, existing_count
    return created_count, existing_count + 1


def main() -> None:
    """CLI entrypoint。API起動からは呼ばず、明示commandだけで実行する。"""

    settings = get_settings()
    logfire.configure(send_to_logfire=settings.logfire_send_to_logfire)
    try:
        result = seed_demo_data(get_engine(), settings)
    except (DemoSeedRefusedError, SQLAlchemyError):
        logfire.exception("local demo seed failed")
        raise
    logfire.info(
        "local demo seed completed",
        event_id=str(DEMO_EVENT_ID),
        created_count=result.created_count,
        existing_count=result.existing_count,
    )


if __name__ == "__main__":
    main()
