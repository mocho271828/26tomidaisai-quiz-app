from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from quiz_app import config as application_config


def test_offline_migration_preserves_saved_client_and_server_policy_lower_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SQL生成はDB接続なし、旧行の移送検証だけを隔離SQLite上で実施する。
    monkeypatch.setattr(
        application_config,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://"),
    )
    output = StringIO()
    config = Config(Path(__file__).resolve().parents[1] / "backend" / "alembic.ini")
    config.output_buffer = output
    command.upgrade(config, "20261015_0005:20261015_0006", sql=True)
    statements = [statement.strip() for statement in output.getvalue().split(";")]
    add_column = next(
        statement[statement.index("ALTER TABLE") :]
        for statement in statements
        if "ALTER TABLE scoring_policies ADD COLUMN client_ahead_tolerance_ms" in statement
    )
    update = next(
        statement for statement in statements if statement.startswith("UPDATE scoring_policies")
    )
    assert update == "UPDATE scoring_policies SET client_ahead_tolerance_ms = clock_tolerance_ms"
    assert "ALTER COLUMN client_ahead_tolerance_ms SET NOT NULL" in output.getvalue()
    assert "CHECK (client_ahead_tolerance_ms >= 0)" in output.getvalue()
    assert (
        "CHECK (timing_basis != 'server_received_v1' OR client_ahead_tolerance_ms = 0)"
        in output.getvalue()
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE scoring_policies "
                "(version TEXT PRIMARY KEY, clock_tolerance_ms INTEGER NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO scoring_policies (version, clock_tolerance_ms) "
                "VALUES (:version, :tolerance)"
            ),
            [{"version": "server-v1", "tolerance": 0}, {"version": "client-v1", "tolerance": 250}],
        )
        connection.execute(text(add_column))
        connection.execute(text(update))
        rows = connection.execute(
            text(
                "SELECT version, clock_tolerance_ms, client_ahead_tolerance_ms "
                "FROM scoring_policies ORDER BY version"
            )
        ).all()
    assert rows == [("client-v1", 250, 250), ("server-v1", 0, 0)]
