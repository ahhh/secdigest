"""Smoke tests for app startup — catches lifespan errors like the passlib/bcrypt crash."""
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_app_lifespan_completes(tmp_db, mock_scheduler):
    """App must start up without raising (the bcrypt crash happened here)."""
    from secdigest.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_with_default_password(tmp_db, mock_scheduler):
    from secdigest.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/login", data={"password": "secdigest"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(tmp_db, mock_scheduler):
    from secdigest.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/login", data={"password": "wrong"})
    assert resp.status_code == 401
