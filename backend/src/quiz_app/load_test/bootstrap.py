from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from quiz_app.config import AppEnvironment, Settings
from quiz_app.dependency_hosts import (
    DependencyHostValidationError,
    ValidatedDependencyHost,
    create_pinned_postgres_engine,
    validate_remote_dependency_host,
)
from quiz_app.load_test.manifest import LoadTestManifest
from quiz_app.load_test.runtime_configuration import matches_formal_quiz_settings
from quiz_app.models import (
    DisplayNamePolicy,
    Event,
    Question,
    QuestionOption,
    QuizRound,
    ScoringPolicy,
)
from quiz_app.models.enums import (
    EventState,
    RankingVisibility,
    RoundResultPublication,
    RoundState,
)
from quiz_app.quiz_configuration import (
    configured_scoring_policy,
    get_quiz_settings,
    initial_event_configuration,
)
from quiz_app.schema_revision import SCHEMA_REVISION

LOAD_TEST_ROUND_SCOPE = "load-test-ten-round"
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "30s"
REQUIRED_LOAD_TEST_TABLES = frozenset(
    {
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


class LoadTestBootstrapRefusedError(RuntimeError):
    """既存dataを変更せず負荷試験Event作成を拒否したことを表す。"""


class LoadTestBootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    event_id: str
    schema_revision: str
    created_events: int
    created_questions: int
    created_options: int
    created_rounds: int


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def validate_bootstrap_settings(settings: Settings, manifest: LoadTestManifest) -> None:
    """負荷試験専用AWS設定とmanifestの対象Event IDが一致することを確認する。"""

    if settings.app_env is not AppEnvironment.AWS_DEMO:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_requires_aws_demo")
    if settings.aws_demo_round_scope != LOAD_TEST_ROUND_SCOPE:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_requires_ten_round_scope")
    if settings.enable_demo_seed:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_requires_demo_seed_disabled")
    if manifest.artifacts.source_commit != settings.app_version:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_source_commit_mismatch")
    allowed_origins = {
        str(origin).removesuffix("/") for origin in settings.realtime_allowed_origins
    }
    if manifest.public_origin not in allowed_origins:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_public_origin_mismatch")
    if settings.aws_demo_expected_event_id != manifest.event.id:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_event_id_mismatch")
    validate_manifest_quiz_configuration(manifest)


def validate_manifest_quiz_configuration(manifest: LoadTestManifest) -> None:
    """DB接続前に単一JSONと試験manifestの採点・進行・公開条件を照合する。"""
    quiz_settings = get_quiz_settings()
    policy = configured_scoring_policy(quiz_settings)
    # v1 manifestは端末時間と猶予を固定できないため、新方式の開催準備には使わない。
    if (
        policy.timing_basis,
        policy.arrival_grace_ms,
        policy.clock_tolerance_ms,
        policy.client_ahead_tolerance_ms,
    ) != (
        "server_received_v1",
        0,
        0,
        0,
    ):
        raise LoadTestBootstrapRefusedError("load_test_quiz_configuration_mismatch")
    expected_policy = policy.model_dump(
        exclude={
            "created_at",
            "timing_basis",
            "arrival_grace_ms",
            "clock_tolerance_ms",
            "client_ahead_tolerance_ms",
        }
    )
    if (
        manifest.event.scoring_policy.model_dump() != expected_policy
        or not matches_formal_quiz_settings(quiz_settings)
    ):
        raise LoadTestBootstrapRefusedError("load_test_quiz_configuration_mismatch")


def _require_future_schedule(manifest: LoadTestManifest, clock: Clock) -> None:
    current_time = clock()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_clock_invalid")
    if manifest.scheduled_start_at <= current_time:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_schedule_not_future")


def _require_remote_database_host(settings: Settings) -> ValidatedDependencyHost:
    try:
        return validate_remote_dependency_host("database", settings.database_url)
    except DependencyHostValidationError as error:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_database_host_invalid") from error


def _advisory_lock_key(_manifest: LoadTestManifest) -> int:
    digest = sha256(b"single-event-load-test-bootstrap").digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _require_current_schema(session: Session) -> None:
    connection = session.connection()
    table_names = set(inspect(connection).get_table_names())
    revisions: list[str] = []
    if "alembic_version" in table_names:
        revisions = list(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
    missing_tables = REQUIRED_LOAD_TEST_TABLES.difference(table_names)
    if missing_tables:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_schema_missing")
    if revisions != [SCHEMA_REVISION]:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_schema_revision_mismatch")


def _require_policies(session: Session, manifest: LoadTestManifest) -> None:
    expected_scoring = manifest.event.scoring_policy
    scoring_policy = session.get(ScoringPolicy, expected_scoring.version)
    if scoring_policy is None:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_scoring_policy_missing")
    if (
        scoring_policy.timing_basis,
        scoring_policy.arrival_grace_ms,
        scoring_policy.clock_tolerance_ms,
        scoring_policy.client_ahead_tolerance_ms,
    ) != ("server_received_v1", 0, 0, 0):
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_scoring_policy_mismatch:timing")
    scoring_fields = (
        "base_points",
        "initial_speed_bonus",
        "speed_bucket_ms",
        "speed_decrement_per_bucket",
        "standard_duration_ms",
        "is_approved",
    )
    scoring_mismatches = [
        field_name
        for field_name in scoring_fields
        if getattr(scoring_policy, field_name) != getattr(expected_scoring, field_name)
    ]
    if scoring_mismatches:
        fields = ",".join(scoring_mismatches)
        raise LoadTestBootstrapRefusedError(f"load_test_bootstrap_scoring_policy_mismatch:{fields}")

    expected_display = manifest.event.display_name_policy
    display_name_policy = session.get(
        DisplayNamePolicy,
        expected_display.version,
    )
    if display_name_policy is None:
        raise LoadTestBootstrapRefusedError("load_test_bootstrap_display_policy_missing")
    display_fields = ("notice_version", "max_code_points", "reject_patterns_json")
    display_mismatches = [
        field_name
        for field_name in display_fields
        if getattr(display_name_policy, field_name) != getattr(expected_display, field_name)
    ]
    if display_mismatches:
        fields = ",".join(display_mismatches)
        raise LoadTestBootstrapRefusedError(f"load_test_bootstrap_display_policy_mismatch:{fields}")


def _has_id_collision(
    session: Session,
    manifest: LoadTestManifest,
) -> bool:
    event_collision = session.scalar(select(Event.id).limit(1))
    question_ids = [question.id for question in manifest.questions]
    option_ids = [option.id for question in manifest.questions for option in question.options]
    question_collision = session.scalar(select(Question.id).where(Question.id.in_(question_ids)))
    option_collision = session.scalar(
        select(QuestionOption.id).where(QuestionOption.id.in_(option_ids))
    )
    round_collision = session.scalar(
        select(QuizRound.id).where(QuizRound.id == manifest.initial_round.id)
    )
    return any(
        collision is not None
        for collision in (
            event_collision,
            question_collision,
            option_collision,
            round_collision,
        )
    )


def _insert_manifest_records(session: Session, manifest: LoadTestManifest) -> None:
    validate_manifest_quiz_configuration(manifest)
    event = Event(
        id=manifest.event.id,
        slug=manifest.event.slug,
        title=manifest.event.title,
        state=EventState.LOBBY_OPEN,
        ranking_visibility=RankingVisibility.HIDDEN_ALL,
        scoring_policy_version=manifest.event.scoring_policy.version,
        display_name_policy_version=manifest.event.display_name_policy.version,
        version=1,
    )
    session.add(event)
    session.flush()
    session.add(initial_event_configuration(event.id, get_quiz_settings()))

    for question_manifest in manifest.questions:
        session.add(
            Question(
                id=question_manifest.id,
                event_id=manifest.event.id,
                version=question_manifest.version,
                prompt=question_manifest.prompt,
                explanation=question_manifest.explanation,
                display_order=question_manifest.display_order,
            )
        )
    session.flush()

    for question_manifest in manifest.questions:
        for option_manifest in question_manifest.options:
            session.add(
                QuestionOption(
                    id=option_manifest.id,
                    question_id=question_manifest.id,
                    label=option_manifest.label,
                    content=option_manifest.content,
                    display_order=option_manifest.display_order,
                    is_correct=option_manifest.label == "B",
                )
            )
    session.flush()

    session.add(
        QuizRound(
            id=manifest.initial_round.id,
            event_id=manifest.event.id,
            question_id=manifest.initial_round.question_id,
            version=manifest.initial_round.version,
            state=RoundState.PREPARING_ASSETS,
            result_publication=RoundResultPublication.WITHHELD,
        )
    )
    session.flush()


def _insert_load_test_event(
    engine: Engine,
    manifest: LoadTestManifest,
) -> LoadTestBootstrapResult:
    try:
        with Session(engine) as session, session.begin():
            session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_lock_key(manifest)},
            )
            _require_current_schema(session)
            _require_policies(session, manifest)
            if _has_id_collision(session, manifest):
                raise LoadTestBootstrapRefusedError("load_test_bootstrap_identity_collision")
            _insert_manifest_records(session, manifest)
    except IntegrityError:
        raise LoadTestBootstrapRefusedError(
            "load_test_bootstrap_concurrent_identity_collision"
        ) from None

    return LoadTestBootstrapResult(
        run_id=str(manifest.run_id),
        event_id=str(manifest.event.id),
        schema_revision=manifest.schema_revision,
        created_events=1,
        created_questions=10,
        created_options=40,
        created_rounds=1,
    )


def bootstrap_load_test_event(
    settings: Settings,
    manifest: LoadTestManifest,
    *,
    clock: Clock = _utc_now,
) -> LoadTestBootstrapResult:
    """検証済みIPへ固定接続し、負荷試験Event一式を1 transactionでINSERTする。"""

    validate_bootstrap_settings(settings, manifest)
    _require_future_schedule(manifest, clock)
    validated_database_host = _require_remote_database_host(settings)
    engine = create_pinned_postgres_engine(
        settings.database_url,
        validated_database_host,
    )
    try:
        return _insert_load_test_event(engine, manifest)
    finally:
        engine.dispose()
