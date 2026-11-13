import json
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel

from quiz_app.bootstrap.demo_content import (
    DEMO_QUESTION_IDS,
    DEMO_QUESTION_SEEDS,
    build_demo_question_seeds,
)
from quiz_app.bootstrap.demo_content_loading import load_demo_content
from quiz_app.bootstrap.demo_seed import (
    DEMO_DATABASE_HOST,
    DEMO_DATABASE_NAME,
    DEMO_DATABASE_PORT,
    DEMO_DATABASE_USERNAME,
    DEMO_EVENT_ID,
    DEMO_EVENT_SLUG,
    DEMO_OPERATOR_ID,
    DEMO_QUESTION_ID,
    DEMO_ROUND_ID,
    DEMO_SCORING_POLICY_VERSION,
    REQUIRED_SEED_TABLES,
    DemoSeedRefusedError,
    DemoSeedResult,
    _reconcile_demo_records,
    _require_current_schema,
    _validate_demo_database_target,
    seed_demo_data,
)
from quiz_app.config import AppEnvironment, Settings
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
from quiz_app.models.enums import EventState, RoundState
from quiz_app.schema_revision import SCHEMA_REVISION

INJECTED_PRODUCTION_VALUE = "injected-production-value"

SeedRecord = (
    AdminAccount
    | DisplayNamePolicy
    | Event
    | EventQuizConfiguration
    | Question
    | QuestionOption
    | QuizRound
    | ScoringPolicy
)


class FakeSeedSession:
    def __init__(self) -> None:
        self.rows: dict[tuple[type[SQLModel], object], SQLModel] = {}

    def get(self, model_type: type[SeedRecord], primary_key: object) -> SeedRecord | None:
        record = self.rows.get((model_type, primary_key))
        return cast(SeedRecord | None, record)

    def scalar(self, statement: object) -> SQLModel | None:
        if "FROM events" in str(statement):
            return next(
                (
                    record
                    for record in self.rows.values()
                    if isinstance(record, Event) and record.id != DEMO_EVENT_ID
                ),
                None,
            )
        return None

    def add(self, record: SeedRecord) -> None:
        primary_key: object
        if isinstance(record, (DisplayNamePolicy, ScoringPolicy)):
            primary_key = record.version
        elif isinstance(record, EventQuizConfiguration):
            primary_key = record.event_id
        else:
            primary_key = record.id
        self.rows[(type(record), primary_key)] = record

    def flush(self) -> None:
        return None


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _reconcile(
    fake_session: FakeSeedSession,
    password_hash_factory: Callable[[], str] | None = None,
    password_hash_matches: Callable[[str], bool] | None = None,
) -> DemoSeedResult:
    return _reconcile_demo_records(
        cast(Session, fake_session),
        _settings(),
        password_hash_factory or (lambda: "$argon2id$fixture"),
        password_hash_matches or (lambda _: True),
    )


def test_demo_questions_keep_existing_identity_independent_of_editable_text() -> None:
    assert [question.version for question in DEMO_QUESTION_SEEDS] == list(range(1, 11))
    assert [str(question.id) for question in DEMO_QUESTION_SEEDS] == [
        f"60000000-0000-4000-8000-{sequence:012d}"
        for sequence in (2, 9, 14, 19, 24, 29, 34, 39, 44, 49)
    ]
    assert [str(option.id) for question in DEMO_QUESTION_SEEDS for option in question.options] == [
        f"60000000-0000-4000-8000-{sequence + offset:012d}"
        for sequence in (2, 9, 14, 19, 24, 29, 34, 39, 44, 49)
        for offset in range(1, 5)
    ]


def test_demo_seed_is_fixed_fictional_and_idempotent_without_resetting_state() -> None:
    session = FakeSeedSession()

    first = _reconcile(session)
    event = session.get(Event, DEMO_EVENT_ID)
    question = session.get(Question, DEMO_QUESTION_ID)
    round_ = session.get(QuizRound, DEMO_ROUND_ID)
    account = session.get(AdminAccount, DEMO_OPERATOR_ID)
    scoring_policy = session.get(ScoringPolicy, DEMO_SCORING_POLICY_VERSION)
    questions = [record for record in session.rows.values() if isinstance(record, Question)]
    options = [record for record in session.rows.values() if isinstance(record, QuestionOption)]

    assert first.created_count == 56
    assert first.existing_count == 0
    assert event is not None
    assert event.slug == DEMO_EVENT_SLUG
    assert event.title.startswith("[DEMO]")
    assert question is not None
    assert question.prompt.startswith("[DEMO]")
    assert round_ is not None
    assert round_.state is RoundState.PREPARING_ASSETS
    assert [item.id for item in sorted(questions, key=lambda item: item.display_order)] == list(
        DEMO_QUESTION_IDS
    )
    assert [
        item.display_order for item in sorted(questions, key=lambda item: item.display_order)[2:]
    ] == list(range(3, 11))
    assert all(item.prompt.startswith("[DEMO]") for item in questions)
    assert len(options) == 40
    for seeded_question in DEMO_QUESTION_SEEDS:
        question_options = [
            option for option in options if option.question_id == seeded_question.id
        ]
        assert len(question_options) == 4
        assert sum(option.is_correct for option in question_options) == 1
    assert account is not None
    assert account.password_hash.startswith("$argon2id$")
    assert scoring_policy is not None
    assert scoring_policy.version == "scoring-2026-10-15-v2"
    assert scoring_policy.timing_basis == "client_monotonic_v1"
    assert scoring_policy.arrival_grace_ms == 4000
    assert scoring_policy.clock_tolerance_ms == 10000
    assert scoring_policy.client_ahead_tolerance_ms == 2500
    assert scoring_policy.base_points == 400
    assert scoring_policy.initial_speed_bonus == 100
    assert scoring_policy.speed_bucket_ms == 100
    assert scoring_policy.speed_decrement_per_bucket == 1
    assert scoring_policy.standard_duration_ms == 10_000

    event.state = EventState.IN_PROGRESS
    round_.question_id = DEMO_QUESTION_IDS[-1]

    def reject_password_rehash() -> str:
        raise AssertionError("既存operatorを再hashしてはいけない")

    second = _reconcile(session, reject_password_rehash)

    assert second.created_count == 0
    assert second.existing_count == 56
    assert event.state is EventState.IN_PROGRESS
    assert round_.question_id == DEMO_QUESTION_IDS[-1]


def test_seed_preserves_manual_visibility_and_refuses_missing_configuration() -> None:
    session = FakeSeedSession()
    _reconcile(session)
    configuration = session.get(EventQuizConfiguration, DEMO_EVENT_ID)
    assert isinstance(configuration, EventQuizConfiguration)
    configuration.distribution_visible = False
    configuration.overall_ranking_visible = True
    configuration.last_applied_round = 3
    _reconcile(session)
    assert not configuration.distribution_visible
    assert configuration.overall_ranking_visible and configuration.last_applied_round == 3
    del session.rows[(EventQuizConfiguration, DEMO_EVENT_ID)]
    with pytest.raises(DemoSeedRefusedError, match="existing_event_configuration_missing"):
        _reconcile(session)


def test_demo_seed_refuses_to_add_seed_beside_another_event() -> None:
    session = FakeSeedSession()
    session.add(
        Event(
            id=UUID("60000000-0000-4000-8000-000000000099"),
            slug="another-event",
            title="別の開催",
            state=EventState.LOBBY_OPEN,
            scoring_policy_version="another-scoring-policy",
            display_name_policy_version="another-display-name-policy",
        )
    )

    with pytest.raises(DemoSeedRefusedError, match="requires_single_event"):
        _reconcile(session)


def test_demo_seed_adds_new_questions_to_legacy_seed_without_resetting_round() -> None:
    session = FakeSeedSession()
    _reconcile(session)
    for key, record in list(session.rows.items()):
        is_new_question = isinstance(record, Question) and record.id != DEMO_QUESTION_ID
        is_new_option = (
            isinstance(record, QuestionOption) and record.question_id != DEMO_QUESTION_ID
        )
        if is_new_question or is_new_option:
            del session.rows[key]

    event = session.get(Event, DEMO_EVENT_ID)
    round_ = session.get(QuizRound, DEMO_ROUND_ID)
    assert event is not None
    assert round_ is not None
    event.state = EventState.IN_PROGRESS
    round_.version = 7

    result = _reconcile(session)

    assert result.created_count == 45
    assert result.existing_count == 11
    assert event.state is EventState.IN_PROGRESS
    assert round_.version == 7


def test_demo_seed_refuses_existing_operator_password_mismatch() -> None:
    session = FakeSeedSession()
    _reconcile(session)

    with pytest.raises(DemoSeedRefusedError, match="operator_credentials_mismatch"):
        _reconcile(session, password_hash_matches=lambda _: False)


def test_demo_seed_rejects_changed_fixed_content() -> None:
    session = FakeSeedSession()
    _reconcile(session)
    question = session.get(Question, DEMO_QUESTION_ID)
    assert question is not None
    question.prompt = "changed"

    with pytest.raises(DemoSeedRefusedError, match="question_mismatch:prompt"):
        _reconcile(session)


def test_edited_json_reaches_seed_but_does_not_overwrite_existing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_session = FakeSeedSession()
    _reconcile(existing_session)
    payload = load_demo_content().model_dump(mode="json")
    payload["questions"][0].update(
        prompt="[DEMO] 架空の季節クイズです。",
        explanation="[DEMO] 正解は冬です。",
        options=["春", "夏", "秋", "冬"],
        correct_label="D",
    )
    path = tmp_path / "demo-questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    edited_seeds = build_demo_question_seeds(load_demo_content(path))
    monkeypatch.setattr("quiz_app.bootstrap.demo_seed.DEMO_QUESTION_SEEDS", edited_seeds)

    fresh_session = FakeSeedSession()
    _reconcile(fresh_session)
    question = fresh_session.get(Question, DEMO_QUESTION_ID)
    assert isinstance(question, Question)
    assert question.prompt == "[DEMO] 架空の季節クイズです。"
    assert question.explanation == "[DEMO] 正解は冬です。"
    for index, seed in enumerate(edited_seeds[0].options):
        option = fresh_session.get(QuestionOption, seed.id)
        assert isinstance(option, QuestionOption)
        assert option.id == DEMO_QUESTION_SEEDS[0].options[index].id
        assert option.label == ("A", "B", "C", "D")[index]
        assert option.content == ("春", "夏", "秋", "冬")[index]
        assert option.display_order == index
        assert option.is_correct == (index == 3)

    with pytest.raises(DemoSeedRefusedError, match="question_mismatch"):
        _reconcile(existing_session)
    original = existing_session.get(Question, DEMO_QUESTION_ID)
    assert isinstance(original, Question)
    assert original.prompt == DEMO_QUESTION_SEEDS[0].prompt


@pytest.mark.parametrize(
    "settings",
    [
        _settings(
            app_env=AppEnvironment.LOCAL_TEST,
            round_backup_storage_mode="disabled",
            round_backup_directory="/var/empty/quiz-round-backups",
            round_backup_encryption_key="disabled",
        ),
        _settings(enable_demo_seed=False),
        _settings(enable_demo_auth=False),
        _settings(database_url="postgresql+psycopg://quiz:quiz@example.test/quiz"),
        _settings(database_url="sqlite:///local.db"),
    ],
)
def test_demo_seed_refuses_unsafe_configuration_before_database_access(
    settings: Settings,
) -> None:
    with pytest.raises(DemoSeedRefusedError):
        seed_demo_data(cast(Engine, object()), settings)


def test_demo_seed_refuses_production_before_database_access() -> None:
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

    with pytest.raises(DemoSeedRefusedError, match="local_demo_environment"):
        seed_demo_data(cast(Engine, object()), production)


def test_demo_seed_target_is_compose_internal_dedicated_database() -> None:
    settings = _settings()

    assert DEMO_DATABASE_HOST == "postgres"
    assert DEMO_DATABASE_PORT == 5432
    assert DEMO_DATABASE_NAME == "quiz_demo"
    assert DEMO_DATABASE_USERNAME == "quiz"
    assert settings.database_url == ("postgresql+psycopg://quiz:quiz@postgres:5432/quiz_demo")
    _validate_demo_database_target(settings.database_url)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://quiz:quiz@localhost:5432/quiz_demo",
        "postgresql+psycopg://quiz:quiz@127.0.0.1:5432/quiz_demo",
        "postgresql+psycopg://quiz:quiz@postgres:55432/quiz_demo",
        "postgresql+psycopg://quiz:quiz@postgres:5432/quiz",
        "postgresql+psycopg://shared:quiz@postgres:5432/quiz_demo",
        "postgresql+psycopg://quiz:quiz@postgres:5432/quiz_demo?sslmode=require",
    ],
)
def test_demo_seed_refuses_non_compose_or_non_dedicated_database_target(
    database_url: str,
) -> None:
    with pytest.raises(DemoSeedRefusedError):
        _validate_demo_database_target(database_url)


def test_demo_seed_requires_current_complete_schema() -> None:
    _require_current_schema(REQUIRED_SEED_TABLES, [SCHEMA_REVISION])

    with pytest.raises(DemoSeedRefusedError, match="schema_missing"):
        _require_current_schema(REQUIRED_SEED_TABLES - {"events"}, [SCHEMA_REVISION])
    with pytest.raises(DemoSeedRefusedError, match="revision_mismatch"):
        _require_current_schema(REQUIRED_SEED_TABLES, ["older_revision"])
    with pytest.raises(DemoSeedRefusedError, match="revision_mismatch"):
        _require_current_schema(REQUIRED_SEED_TABLES, [SCHEMA_REVISION, "other_head"])
