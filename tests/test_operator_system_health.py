import pytest
from httpx import ASGITransport, AsyncClient

from quiz_app.main import create_app


@pytest.mark.asyncio
async def test_removed_monitoring_endpoint_is_not_exposed() -> None:
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/operator/system-health")
    assert response.status_code == 404
    schema = application.openapi()
    assert "/api/v1/operator/system-health" not in schema["paths"]
    assert "SystemHealthResponse" not in schema["components"]["schemas"]
