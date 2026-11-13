import logfire
from fastapi import Request
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.contracts import ReadinessHealthResponse, RedisDependencyStatus
from quiz_app.db import get_engine
from quiz_app.realtime.redis_adapter import get_redis_health_probe
from quiz_app.schema_revision import SCHEMA_REVISION


def inspect_readiness(
    engine: Engine,
    redis_status: RedisDependencyStatus = "ok",
) -> ReadinessHealthResponse:
    """DB接続とAlembic revisionを検査し、秘密情報を含まない状態を返す。"""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            schema_inspector = inspect(connection)
            if not schema_inspector.has_table("alembic_version"):
                return ReadinessHealthResponse(
                    status="not_ready",
                    database="ok",
                    migration="missing",
                    redis=redis_status,
                )
            current_revisions = (
                connection.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 2"),
                )
                .scalars()
                .all()
            )
    except SQLAlchemyError:
        logfire.exception("readiness database check failed")
        return ReadinessHealthResponse(
            status="not_ready",
            database="unavailable",
            migration="unknown",
            redis=redis_status,
        )

    if len(current_revisions) != 1:
        return ReadinessHealthResponse(
            status="not_ready",
            database="ok",
            migration="missing" if not current_revisions else "unknown",
            redis=redis_status,
        )

    if current_revisions[0] != SCHEMA_REVISION:
        return ReadinessHealthResponse(
            status="not_ready",
            database="ok",
            migration="outdated",
            redis=redis_status,
        )

    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("events"):
                return ReadinessHealthResponse(
                    status="not_ready",
                    database="ok",
                    migration="missing",
                    redis=redis_status,
                )
            event_count = connection.execute(
                text("SELECT COUNT(*) FROM (SELECT 1 FROM events LIMIT 2) configured_events")
            ).scalar_one()
    except SQLAlchemyError:
        logfire.exception("readiness single event check failed")
        return ReadinessHealthResponse(
            status="not_ready",
            database="unavailable",
            migration="current",
            redis=redis_status,
        )

    if event_count != 1:
        return ReadinessHealthResponse(
            status="not_ready",
            database="ok",
            migration="current",
            redis=redis_status,
        )

    return ReadinessHealthResponse(
        status="ready" if redis_status == "ok" else "degraded",
        database="ok",
        migration="current",
        redis=redis_status,
    )


def check_readiness(request: Request) -> ReadinessHealthResponse:
    """FastAPI dependencyとしてprocess共通engineのreadinessを返す。"""

    redis_status = get_redis_health_probe(request).status()
    readiness = inspect_readiness(get_engine(), redis_status)
    if getattr(request.app.state, "automatic_result_worker_alive", True) is False:
        return readiness.model_copy(update={"status": "not_ready"})
    return readiness
