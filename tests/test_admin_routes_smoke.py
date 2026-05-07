"""Smoke tests for the admin app: every authed GET route renders without 500.

This catches the cheap regressions — import errors after a refactor, template
syntax mistakes, missing route registrations — without needing per-page assertions.
Add a route to the admin app, add it to the list here.
"""
import pytest

from secdigest import db


# Routes that take a parameter need a known seeded value. The list below is paired
# with `seed` lambdas where needed.
STATIC_ROUTES = [
    "/",                          # redirects to /day/<today> when authed
    "/archive",
    "/subscribers",
    "/prompts",
    "/feeds",
    "/settings",
    "/email-templates",
]


def _seed_minimal(date_str: str = "2026-05-04"):
    """Seed enough state that parameterised routes don't 500 on first hit."""
    n = db.newsletter_get_or_create(date_str)
    aid = db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="seed",
        url="https://example.invalid/seed",
        hn_score=0, hn_comments=0, relevance_score=7.0,
        relevance_reason="seed", position=0, included=1,
    )
    return n, aid


@pytest.mark.parametrize("path", STATIC_ROUTES)
async def test_admin_static_route_renders(admin_client, path):
    r = await admin_client.get(path)
    assert r.status_code in (200, 302), \
        f"GET {path} returned {r.status_code}; body: {r.text[:300]}"


async def test_admin_day_view_renders(admin_client):
    _seed_minimal("2026-05-04")
    r = await admin_client.get("/day/2026-05-04")
    assert r.status_code == 200, r.text[:300]
    assert "seed" in r.text


async def test_admin_day_view_renders_for_empty_date(admin_client):
    """Visiting a date with no newsletter must not 500 — it should render the
    empty-state UI."""
    r = await admin_client.get("/day/2099-12-31")
    assert r.status_code == 200


async def test_admin_pool_view_renders(admin_client):
    _seed_minimal("2026-05-04")
    r = await admin_client.get("/day/2026-05-04/pool")
    assert r.status_code == 200


async def test_admin_week_digest_auto_seeds(admin_client):
    _seed_minimal("2026-05-04")
    # /week/{monday} auto-creates the digest row + seeds the join table on first hit
    r = await admin_client.get("/week/2026-05-04")
    assert r.status_code == 200
    assert "Weekly digest" in r.text


async def test_admin_month_digest_auto_seeds(admin_client):
    _seed_minimal("2026-05-04")
    r = await admin_client.get("/month/2026-05-01")
    assert r.status_code == 200
    assert "Monthly digest" in r.text


async def test_admin_login_page_unauthed():
    """The login page itself must always render — no fixture needed beyond a
    fresh DB. We can't use admin_client (it requires auth)."""
    from httpx import AsyncClient, ASGITransport
    from secdigest.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/login")
    assert r.status_code == 200


async def test_admin_route_index_redirects_when_unauthed(tmp_db, mock_scheduler):
    """Hitting / without a session must redirect to /login, never 500."""
    from httpx import AsyncClient, ASGITransport
    from secdigest.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           follow_redirects=False) as c:
        r = await c.get("/")
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")
