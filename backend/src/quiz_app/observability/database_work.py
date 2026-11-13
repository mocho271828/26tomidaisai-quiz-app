"""実行SQLの本文・引数を保持しない、要求単位の件数計測。"""

from sqlalchemy import Connection, Engine, event
from sqlalchemy.engine import ExecutionContext

from quiz_app.observability.request_timing import record_database_work


def _record_statement(
    _connection: Connection,
    _cursor: object,
    _statement: str,
    _parameters: object,
    _context: ExecutionContext,
    _executemany: bool,
) -> None:
    record_database_work("statements")


def observe_database_statements(engine: Engine) -> None:
    """async engineにもsync_engineの公式event境界で同じ件数を記録する。"""

    event.listen(engine, "before_cursor_execute", _record_statement)
