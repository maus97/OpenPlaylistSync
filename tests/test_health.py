import httpx
import pytest

from ops.main import app


@pytest.mark.asyncio
async def test_healthz_reports_service_status() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "open-playlist-sync"}
