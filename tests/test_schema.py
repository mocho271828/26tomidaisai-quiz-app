from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlmodel import SQLModel

import quiz_app.models  # noqa: F401  # metadata登録を検査するため全modelをimportする。
from quiz_app.health import inspect_readiness
from quiz_app.schema_revision import SCHEMA_REVISION


def test_metadata_contains_required_tables_and_constraints() -> None:
    required_tables = {
        "admin_accounts",
        "admin_sessions",
        "answers",
        "events",
        "idempotency_records",
        "operation_audits",
        "participants",
        "participant_sessions",
        "question_options",
        "questions",
        "quiz_rounds",
        "rank_snapshot_rows",
        "rank_snapshots",
        "readiness_reports",
        "round_preparation_targets",
        "round_option_stats",
        "score_ledgers",
    }

    assert required_tables <= set(SQLModel.metadata.tables)

    answer_constraints = {
        constraint.name for constraint in SQLModel.metadata.tables["answers"].constraints
    }
    assert "uq_answers_participant_round" in answer_constraints
    assert "uq_answers_participant_client_id" in answer_constraints
    assert "ck_answers_client_elapsed" in answer_constraints
    assert SQLModel.metadata.tables["answers"].c.client_elapsed_ms.nullable

    policy_columns = SQLModel.metadata.tables["scoring_policies"].c
    for column in (
        "timing_basis",
        "arrival_grace_ms",
        "clock_tolerance_ms",
        "client_ahead_tolerance_ms",
    ):
        assert column in policy_columns
        assert not policy_columns[column].nullable

    policy_constraints = {
        constraint.name for constraint in SQLModel.metadata.tables["scoring_policies"].constraints
    }
    assert "ck_scoring_policies_client_ahead_tolerance" in policy_constraints
    assert "ck_scoring_policies_legacy_client_ahead" in policy_constraints

    participant_constraints = {
        constraint.name for constraint in SQLModel.metadata.tables["participants"].constraints
    }
    assert "uq_participants_event_join_attempt_hash" in participant_constraints

    answer_default = SQLModel.metadata.tables["answers"].c.received_at.server_default
    assert answer_default is not None
    assert "clock_timestamp()" in str(answer_default.arg)

    correct_indexes = {index.name for index in SQLModel.metadata.tables["question_options"].indexes}
    assert "uq_question_options_one_correct" in correct_indexes

    score_columns = SQLModel.metadata.tables["score_ledgers"].c
    assert "speed_rank" in score_columns
    assert "speed_cohort_size" in score_columns
    assert "is_demo" in SQLModel.metadata.tables["admin_accounts"].c


def test_alembic_head_matches_application_revision() -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(project_root / "backend" / "alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == SCHEMA_REVISION


def test_readiness_reports_missing_current_and_outdated_schema() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    missing = inspect_readiness(engine)
    assert missing.status == "not_ready"
    assert missing.database == "ok"
    assert missing.migration == "missing"

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": "older_revision"},
        )

    outdated = inspect_readiness(engine)
    assert outdated.status == "not_ready"
    assert outdated.migration == "outdated"

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": SCHEMA_REVISION},
        )

    missing_event_table = inspect_readiness(engine)
    assert missing_event_table.status == "not_ready"
    assert missing_event_table.migration == "missing"

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id TEXT PRIMARY KEY)"))

    no_event = inspect_readiness(engine)
    assert no_event.status == "not_ready"
    assert no_event.database == "ok"
    assert no_event.migration == "current"

    with engine.begin() as connection:
        connection.execute(text("INSERT INTO events (id) VALUES ('event-1')"))

    current = inspect_readiness(engine)
    assert current.status == "ready"
    assert current.migration == "current"

    degraded = inspect_readiness(engine, "degraded")
    assert degraded.status == "degraded"
    assert degraded.database == "ok"
    assert degraded.redis == "degraded"

    with engine.begin() as connection:
        connection.execute(text("INSERT INTO events (id) VALUES ('event-2')"))

    multiple_events = inspect_readiness(engine)
    assert multiple_events.status == "not_ready"
    assert multiple_events.database == "ok"
    assert multiple_events.migration == "current"

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": "unexpected_second_revision"},
        )

    multiple = inspect_readiness(engine)
    assert multiple.status == "not_ready"
    assert multiple.database == "ok"
    assert multiple.migration == "unknown"
