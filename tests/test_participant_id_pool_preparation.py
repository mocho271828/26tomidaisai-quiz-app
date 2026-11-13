import re
import stat
import sys
from itertools import product
from pathlib import Path
from string import ascii_uppercase
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

import quiz_app.bootstrap.participant_id_pool_sql as preparation
from quiz_app.bootstrap.participant_id_pool_sql import (
    PoolSqlRequest,
    build_pool_preparation_sql,
    build_pool_preparation_statement,
    write_pool_preparation_sql,
)
from quiz_app.models.participant_ids import ParticipantIdPool, ParticipantIdSlot

EVENT_ID = UUID("70000000-0000-4000-8000-000000000001")


def test_driver_statement_keeps_commit_and_psql_commands_with_the_caller() -> None:
    statement = build_pool_preparation_statement(EVENT_ID).as_string()
    assert statement.startswith("SET LOCAL lock_timeout")
    assert "\\set" not in statement
    assert "COMMIT;" not in statement
    assert "CREATE SEQUENCE" in statement
    assert "participant_id_pool_schema_revision_mismatch" in statement


def test_generated_pool_has_every_three_letter_id_once() -> None:
    sql = build_pool_preparation_sql(EVENT_ID)
    rows = re.findall(r"^\((\d+), '([A-Z]{3})'\)[,;]$", sql, re.MULTILINE)
    labels = [label for _, label in rows]

    assert [int(ordinal) for ordinal, _ in rows] == list(range(1, 17_577))
    assert set(labels) == {"".join(value) for value in product(ascii_uppercase, repeat=3)}
    assert len(labels) == len(set(labels)) == 17_576
    assert labels != sorted(labels)


def test_preparation_guards_existing_pool_and_preserves_existing_participants() -> None:
    sql = build_pool_preparation_sql(EVENT_ID)

    assert f"WHERE id = '{EVENT_ID.hex}'::uuid FOR UPDATE" in sql
    assert "participant_id_pool_event_missing" in sql
    assert "participant_id_pool_already_prepared" in sql
    assert f"to_regclass('public.participant_id_{EVENT_ID.hex}')" in sql
    assert "existing.public_label = candidate.public_label" in sql
    assert "row_number() OVER (ORDER BY candidate.shuffle_order)" in sql
    assert "IF pool_capacity > 0 THEN" in sql
    assert "MINVALUE 1 MAXVALUE %s" in sql
    assert "START WITH 1 INCREMENT BY 1 CACHE 1 NO CYCLE" in sql
    assert sql.index("FOR UPDATE") < sql.index("INSERT INTO public.participant_id_pools")
    assert not re.search(r"\b(?:UPDATE|DELETE FROM|TRUNCATE) public\.", sql)
    assert "setval(" not in sql
    assert "nextval(" not in sql
    assert "CREATE SEQUENCE IF NOT EXISTS" not in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_write_refuses_existing_file_and_does_not_change_its_contents(tmp_path: Path) -> None:
    output = tmp_path / "prepare.sql"
    request = PoolSqlRequest(event_id=EVENT_ID, output=output)
    write_pool_preparation_sql(request)
    original = output.read_bytes()

    with pytest.raises(FileExistsError):
        write_pool_preparation_sql(request)

    assert output.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_write_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "original.sql"
    target.write_text("keep", encoding="utf-8")
    output = tmp_path / "prepare.sql"
    output.symlink_to(target)

    with pytest.raises(FileExistsError):
        write_pool_preparation_sql(PoolSqlRequest(event_id=EVENT_ID, output=output))

    assert target.read_text(encoding="utf-8") == "keep"


def test_sql_generation_rejects_unvalidated_event_input(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        PoolSqlRequest.model_validate({"event_id": "x'; DROP TABLE events;--", "output": tmp_path})


def test_pool_metadata_enforces_bounds_format_and_unique_slots() -> None:
    pool_table = ParticipantIdPool.__table__
    slot_table = ParticipantIdSlot.__table__

    assert {column.name for column in pool_table.primary_key.columns} == {"event_id"}
    assert {column.name for column in slot_table.primary_key.columns} == {"event_id", "ordinal"}
    assert {constraint.name for constraint in pool_table.constraints} >= {
        "ck_participant_id_pools_capacity",
        "ck_participant_id_pools_version",
    }
    assert {constraint.name for constraint in slot_table.constraints} >= {
        "uq_participant_id_slots_event_label",
        "ck_participant_id_slots_ordinal",
        "ck_participant_id_slots_label",
    }

    postgres_ddl = str(CreateTable(slot_table).compile(dialect=postgresql.dialect()))
    sqlite_fixture_ddl = str(CreateTable(slot_table).compile(dialect=sqlite.dialect()))
    assert "public_label ~ '^[A-Z]{3}$'" in postgres_ddl
    assert "public_label ~" not in sqlite_fixture_ddl


def test_cli_generates_sql_without_opening_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "prepare.sql"

    def reject_connection(*_args: object, **_kwargs: object) -> None:
        pytest.fail("SQL artifact generation must not connect to a database")

    monkeypatch.setattr("psycopg.connect", reject_connection)
    monkeypatch.setattr("sqlalchemy.create_engine", reject_connection)
    monkeypatch.setattr(preparation.logfire, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(preparation.logfire, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys, "argv", ["participant-id-pool", "--event-id", str(EVENT_ID), "--output", str(output)]
    )

    preparation.main()

    assert output.read_text(encoding="utf-8").endswith("COMMIT;\n")
