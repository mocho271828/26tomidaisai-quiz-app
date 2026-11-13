import pytest
from httpx import ASGITransport, AsyncClient

from quiz_app.main import app


@pytest.mark.asyncio
async def test_liveness_does_not_expose_dependency_details() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "quiz-api",
        "version": "0.1.0",
    }
