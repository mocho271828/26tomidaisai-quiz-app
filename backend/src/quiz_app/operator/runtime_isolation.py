from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from quiz_app.config import AppEnvironment, Settings
from quiz_app.models.identity import AdminAccount


def enforce_production_runtime_isolation(settings: Settings, engine: Engine) -> None:
    """production processがdemo accountを含むDBへ接続することを拒否する。"""

    if settings.app_env is not AppEnvironment.PRODUCTION:
        return

    try:
        with Session(engine) as session:
            demo_account_count = session.scalar(
                select(func.count()).select_from(AdminAccount).where(AdminAccount.is_demo.is_(True))
            )
    except SQLAlchemyError as error:
        raise RuntimeError("production_runtime_isolation_check_failed") from error

    if demo_account_count is None:
        raise RuntimeError("production_runtime_isolation_check_failed")
    if demo_account_count > 0:
        raise RuntimeError("production_database_contains_demo_accounts")
