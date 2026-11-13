from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from quiz_app.contracts import ReadinessHealthResponse
from quiz_app.health import check_readiness
from quiz_app.main import app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_readiness_returns_503_when_schema_is_missing(client: AsyncClient) -> None:
    app.dependency_overrides[check_readiness] = lambda: ReadinessHealthResponse(
        status="not_ready",
        database="ok",
        migration="missing",
        redis="ok",
    )

    response = await client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "ok",
        "migration": "missing",
        "redis": "ok",
    }


@pytest.mark.asyncio
async def test_readiness_returns_200_at_current_revision(client: AsyncClient) -> None:
    app.dependency_overrides[check_readiness] = lambda: ReadinessHealthResponse(
        status="ready",
        database="ok",
        migration="current",
        redis="ok",
    )

    response = await client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "migration": "current",
        "redis": "ok",
    }


@pytest.mark.asyncio
async def test_readiness_returns_200_when_only_redis_is_degraded(client: AsyncClient) -> None:
    app.dependency_overrides[check_readiness] = lambda: ReadinessHealthResponse(
        status="degraded",
        database="ok",
        migration="current",
        redis="degraded",
    )

    response = await client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "database": "ok",
        "migration": "current",
        "redis": "degraded",
    }
