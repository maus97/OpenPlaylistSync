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


@pytest.mark.asyncio
async def test_state_changing_forms_require_session_csrf_token() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        rejected = await client.post("/pairs/999/toggle")
        page = await client.get("/settings")
        token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        accepted = await client.post("/pairs/999/toggle", data={"csrf_token": token})

    assert rejected.status_code == 403
    assert accepted.status_code == 404
