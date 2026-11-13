"""既存DB権限の範囲で識別情報を除いた待機状態を読み取る。"""

import json
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.db import get_engine


class DatabaseWaitSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "unavailable"]
    lock_waiters: int = Field(default=0, ge=0)
    row_lock_waiters: int = Field(default=0, ge=0)
    active_connections: int = Field(default=0, ge=0)
    tls_connections: int = Field(default=0, ge=0)
    observed_connections: int = Field(default=0, ge=0)
    longest_blocked_statement_seconds: float = Field(default=0, ge=0)
    sample_seconds: float = Field(ge=0)


def read_database_waits() -> DatabaseWaitSnapshot:
    """query本文を取得せず集約する。blocked statement経過はlock待ちの上限値。"""

    started = monotonic()
    try:
        with get_engine().connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            row = (
                connection.execute(
                    text("""
                SELECT COUNT(*) FILTER (WHERE a.wait_event_type = 'Lock') AS lock_waiters,
                       COUNT(*) FILTER (WHERE a.wait_event_type = 'Lock'
                         AND a.wait_event IN ('tuple', 'transactionid')) AS row_lock_waiters,
                       COUNT(*) FILTER (WHERE a.state = 'active') AS active_connections,
                       COUNT(*) FILTER (WHERE s.ssl) AS tls_connections,
                       COUNT(*) AS observed_connections,
                       COALESCE(MAX(EXTRACT(EPOCH FROM clock_timestamp() - a.query_start))
                         FILTER (WHERE a.wait_event_type = 'Lock'), 0)
                         AS longest_blocked_statement_seconds
                FROM pg_stat_activity a LEFT JOIN pg_stat_ssl s USING (pid)
                WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
            """)
                )
                .mappings()
                .one()
            )
            return DatabaseWaitSnapshot.model_validate(
                {
                    **row,
                    "status": "ok",
                    "sample_seconds": monotonic() - started,
                }
            )
    except SQLAlchemyError:
        # driver例外には接続資格情報が含まれ得るため、raw例外をartifactへ出さない。
        return DatabaseWaitSnapshot(status="unavailable", sample_seconds=monotonic() - started)


if __name__ == "__main__":
    print(json.dumps(read_database_waits().model_dump()))
