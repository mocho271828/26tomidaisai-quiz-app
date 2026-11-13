from typing import cast

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, PoolProxiedConnection
from sqlmodel import Session
from sqlmodel.ext.asyncio.session import AsyncSession

from quiz_app import async_db
from quiz_app.config import Settings
from quiz_app.observability.request_timing import RequestTiming, current_request_timing


@pytest.mark.asyncio
async def test_async_engine_uses_psycopg_tls_and_bounded_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://fixture:fixture@localhost/fixture?sslmode=verify-full",
        database_pool_size=7,
        database_max_overflow=0,
        database_pool_timeout_seconds=12,
    )
    monkeypatch.setattr(async_db, "get_settings", lambda: settings)
    async_db.get_async_engine.cache_clear()
    engine = async_db.get_async_engine()
    try:
        assert engine is async_db.get_async_engine()
        assert engine.dialect.is_async is True
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.query["sslmode"] == "verify-full"
        assert isinstance(engine.pool, async_db.ObservedAsyncQueuePool)
        assert engine.pool.size() == 7
        assert engine.pool.timeout() == 12
    finally:
        await engine.dispose()
        async_db.get_async_engine.cache_clear()


@pytest.mark.asyncio
async def test_factory_provides_sqlmodel_session_to_run_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("postgresql+psycopg://fixture:fixture@localhost/fixture")
    monkeypatch.setattr(async_db, "get_async_engine", lambda: engine)
    factory = async_db.get_async_sessionmaker()
    try:
        async with factory() as first, factory() as second:
            assert isinstance(first, AsyncSession)
            assert first is not second
            assert first.sync_session.expire_on_commit is False
            assert await first.run_sync(lambda session: isinstance(session, Session)) is True
    finally:
        await engine.dispose()


@pytest.mark.parametrize("fails", [False, True])
def test_async_pool_records_acquisition_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    pool = async_db.ObservedAsyncQueuePool(lambda: None)
    connection = cast(PoolProxiedConnection, object())

    def connect(_pool: AsyncAdaptedQueuePool) -> PoolProxiedConnection:
        if fails:
            raise TimeoutError("fixture acquisition failure")
        return connection

    monkeypatch.setattr(AsyncAdaptedQueuePool, "connect", connect)
    times = iter((1.0, 1.25))
    monkeypatch.setattr(async_db, "monotonic", lambda: next(times))
    timing = RequestTiming()
    token = current_request_timing.set(timing)
    try:
        if fails:
            with pytest.raises(TimeoutError, match="fixture acquisition failure"):
                pool.connect()
        else:
            assert pool.connect() is connection
        assert timing.durations["db_acquire"] == 0.25
    finally:
        current_request_timing.reset(token)
