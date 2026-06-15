"""Tests for the weekly/monthly digest curator routes.

The existing test suite only smoke-tested the GET view and never exercised the
POST actions (toggle include/exclude, reorder). Both had silent bugs that these
tests are designed to catch:

  - toggle: inner <form> elements nested inside the outer reorder <form> were
    ignored by the HTML parser, so clicking Exclude submitted the reorder form
    instead and the included state never changed.
  - reorder: not tested at all; DB persistence could have regressed undetected.
"""
import pytest

from secdigest import db
from tests.conftest import get_csrf


def _seed_week(date_str: str = "2026-05-04") -> tuple[int, int]:
    """Insert two articles into a daily newsletter that falls inside the
    ISO week containing date_str. Returns (article_id_1, article_id_2)."""
    n = db.newsletter_get_or_create(date_str)
    aid1 = db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="alpha article",
        url="https://example.invalid/1", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="seed", position=0, included=1,
    )
    aid2 = db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="beta article",
        url="https://example.invalid/2", hn_score=0, hn_comments=0,
        relevance_score=8.0, relevance_reason="seed", position=1, included=1,
    )
    return aid1, aid2


async def test_week_article_toggle_flips_included_state(admin_client):
    """POSTing to the digest toggle endpoint changes the article's included flag."""
    _seed_week("2026-05-04")
    # Visiting auto-seeds the digest_articles join table
    r = await admin_client.get("/week/2026-05-04")
    assert r.status_code == 200

    digest = db.newsletter_get("2026-05-04", kind="weekly")
    articles = db.digest_article_list(digest["id"])
    assert articles, "digest was not seeded with articles"

    target = articles[0]
    included_before = target["included"]

    tok = await get_csrf(admin_client, "/week/2026-05-04")
    r = await admin_client.post(
        f"/week/2026-05-04/article/{target['id']}/toggle",
        data={"csrf_token": tok},
    )
    assert r.status_code == 302, f"expected redirect, got {r.status_code}: {r.text[:200]}"

    refreshed = db.digest_article_list(digest["id"])
    included_after = next(a["included"] for a in refreshed if a["id"] == target["id"])
    assert included_after != included_before, (
        "toggle POST did not change the included state — "
        "likely the inner <form> was nested inside the reorder form and its "
        "action was overridden by the outer form"
    )


async def test_week_article_toggle_round_trips(admin_client):
    """Toggling twice restores the original included state."""
    _seed_week("2026-05-04")
    await admin_client.get("/week/2026-05-04")

    digest = db.newsletter_get("2026-05-04", kind="weekly")
    articles = db.digest_article_list(digest["id"])
    target = articles[0]
    original = target["included"]

    for _ in range(2):
        tok = await get_csrf(admin_client, "/week/2026-05-04")
        await admin_client.post(
            f"/week/2026-05-04/article/{target['id']}/toggle",
            data={"csrf_token": tok},
        )

    refreshed = db.digest_article_list(digest["id"])
    after = next(a["included"] for a in refreshed if a["id"] == target["id"])
    assert after == original, "double-toggle did not restore original included state"


async def test_week_curator_page_reflects_excluded_state(admin_client):
    """After a toggle the curator GET renders the article with the excluded CSS class."""
    _seed_week("2026-05-04")
    await admin_client.get("/week/2026-05-04")

    digest = db.newsletter_get("2026-05-04", kind="weekly")
    articles = db.digest_article_list(digest["id"])
    target = articles[0]
    assert target["included"], "pre-condition: article should start as included"

    tok = await get_csrf(admin_client, "/week/2026-05-04")
    r = await admin_client.post(
        f"/week/2026-05-04/article/{target['id']}/toggle",
        data={"csrf_token": tok},
    )
    assert r.status_code == 302
    location = r.headers.get("location", "/week/2026-05-04")
    page = await admin_client.get(location)
    assert page.status_code == 200
    assert "article-excluded" in page.text, (
        "curator page did not render the excluded article with the "
        "article-excluded CSS class after toggle"
    )


async def test_week_article_reorder_persists_to_db(admin_client):
    """POSTing a reversed order list to /reorder saves the new positions."""
    _seed_week("2026-05-04")
    await admin_client.get("/week/2026-05-04")

    digest = db.newsletter_get("2026-05-04", kind="weekly")
    articles = db.digest_article_list(digest["id"])
    assert len(articles) >= 2, "need at least 2 articles to verify reorder"

    reversed_ids = [str(a["id"]) for a in reversed(articles)]
    tok = await get_csrf(admin_client, "/week/2026-05-04")
    r = await admin_client.post(
        "/week/2026-05-04/reorder",
        data={"csrf_token": tok, "order": reversed_ids},
    )
    assert r.status_code == 302, f"expected redirect, got {r.status_code}: {r.text[:200]}"

    reloaded = db.digest_article_list(digest["id"])
    reloaded_ids = [str(a["id"]) for a in reloaded]
    assert reloaded_ids == reversed_ids, (
        f"reorder was not persisted: got {reloaded_ids}, expected {reversed_ids}"
    )


async def test_month_article_toggle_flips_included_state(admin_client):
    """Same toggle check for the monthly digest route."""
    _seed_week("2026-05-01")
    await admin_client.get("/month/2026-05-01")

    digest = db.newsletter_get("2026-05-01", kind="monthly")
    articles = db.digest_article_list(digest["id"])
    assert articles, "monthly digest was not seeded"

    target = articles[0]
    included_before = target["included"]

    tok = await get_csrf(admin_client, "/month/2026-05-01")
    r = await admin_client.post(
        f"/month/2026-05-01/article/{target['id']}/toggle",
        data={"csrf_token": tok},
    )
    assert r.status_code == 302

    refreshed = db.digest_article_list(digest["id"])
    included_after = next(a["included"] for a in refreshed if a["id"] == target["id"])
    assert included_after != included_before
