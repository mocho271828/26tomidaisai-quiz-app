from functools import lru_cache

from sqlalchemy import Engine, create_engine

from quiz_app.config import get_settings
from quiz_app.observability.database_work import observe_database_statements
from quiz_app.observability.runtime import ObservedQueuePool


@lru_cache
def get_engine() -> Engine:
    """PostgreSQL engineを生成し、process内で共有する。"""

    settings = get_settings()
    return create_sync_engine(
        pool_size=settings.database_sync_pool_size,
        max_overflow=settings.database_sync_max_overflow,
    )


def create_sync_engine(*, pool_size: int, max_overflow: int = 0) -> Engine:
    """用途ごとに接続予算を固定し、overflow接続の再生成を避ける。"""

    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        poolclass=ObservedQueuePool,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    observe_database_statements(engine)
    return engine
