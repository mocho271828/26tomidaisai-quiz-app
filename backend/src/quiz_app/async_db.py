"""参加者HTTPで共有するasync DB poolとSQLModel Session境界。"""

from functools import lru_cache
from time import monotonic

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, PoolProxiedConnection
from sqlmodel.ext.asyncio.session import AsyncSession

from quiz_app.config import get_settings
from quiz_app.observability.database_work import observe_database_statements
from quiz_app.observability.metrics import QuizMetrics
from quiz_app.observability.request_timing import record_database_work, record_timing


class ObservedAsyncQueuePool(AsyncAdaptedQueuePool):
    """async pool取得全体を計測し、純粋なqueue待ちとは区別する。"""

    quiz_metrics: QuizMetrics | None = None

    def connect(self) -> PoolProxiedConnection:
        started = monotonic()
        record_database_work("acquisitions")
        succeeded = False
        try:
            connection = super().connect()
            succeeded = True
            return connection
        finally:
            elapsed = monotonic() - started
            record_timing("db_acquire", elapsed)
            if self.quiz_metrics is not None:
                self.quiz_metrics.database_acquisition.labels(
                    result="success" if succeeded else "error"
                ).observe(elapsed)


@lru_cache
def get_async_engine() -> AsyncEngine:
    """既存psycopgとTLS設定を使い、async HTTP専用poolをprocess内で共有する。"""

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        poolclass=ObservedAsyncQueuePool,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    observe_database_statements(engine.sync_engine)
    return engine


def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """各requestに独立したSessionを作り、commit後の暗黙I/Oを避ける。"""

    return async_sessionmaker(
        get_async_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )
