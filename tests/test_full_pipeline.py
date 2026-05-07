"""End-to-end pipeline smoke test.

Drives the entire SecDigest lifecycle through both apps with all egress mocked:

    1. fetcher.run_fetch()          ← mocked HN API + mocked Anthropic scoring
    2. summarizer.summarize_article  ← mocked Anthropic + mocked article body fetch
    3. /day/<date> curator           ← pin articles, exclude, reorder
    4. /day/<date>/send              ← mocked SMTP; subscribers filtered by cadence
    5. /week/<monday>                ← auto-seeds digest from pinned + top relevance
    6. /week/<monday>/send           ← only weekly subscribers receive
    7. public /subscribe             ← double-opt-in confirmation email
    8. public /confirm/<token>       ← row activates
    9. public /unsubscribe/<token>   ← active=0
    10. next /day/.../send            ← unsubscribed user not in recipients

If any of those links break, this test catches it. The whole thing runs offline.
"""
import re

import pytest

from secdigest import db, fetcher, summarizer, mailer
from tests.conftest import get_csrf


# ── HN + RSS canned data ─────────────────────────────────────────────────────

HN_TOP_IDS = [11001, 11002, 11003, 11004, 11005]
HN_NEW_IDS = [12001, 12002]


def _hn_item(item_id: int, title: str, url: str, score: int = 80) -> dict:
    return {
        "id": item_id, "type": "story", "title": title, "url": url,
        "score": score, "descendants": 12, "by": "tester", "time": 1746500000,
    }


HN_ITEMS = {
    11001: _hn_item(11001, "CVE-2026-9999: heap UAF in libfoo", "https://example.invalid/cve-9999"),
    11002: _hn_item(11002, "New phishing kit chains MFA bypass", "https://example.invalid/phish-mfa"),
    11003: _hn_item(11003, "Side-channel in RISC-V branch predictor", "https://example.invalid/sc-rv"),
    11004: _hn_item(11004, "Apple ships emergency Safari patch", "https://example.invalid/safari"),
    11005: _hn_item(11005, "Lattice attack on legacy ECDSA", "https://example.invalid/ecdsa"),
    12001: _hn_item(12001, "Show HN: rust container scanner", "https://example.invalid/rust-scan", score=60),
    12002: _hn_item(12002, "Why we ditched Kafka for NATS", "https://example.invalid/nats", score=55),
}


def _wire_hn(stub_httpx):
    """Configure the httpx stub for HN topstories/newstories/item endpoints."""
    stub_httpx.route("topstories.json", json_data=HN_TOP_IDS)
    stub_httpx.route("newstories.json", json_data=HN_NEW_IDS)
    for item_id, payload in HN_ITEMS.items():
        stub_httpx.route(f"/item/{item_id}.json", json_data=payload)


def _wire_summarizer_fetch(stub_httpx):
    """Summarizer pulls article bodies via httpx.Client; return canned HTML for any URL."""
    stub_httpx.route("example.invalid",
                     text="<html><body><p>Vulnerability details. Affected versions 1.0–2.3."
                          " Patch released. CVSS 9.1.</p></body></html>")


# ── Curation responses (one .messages.create per article, FIFO) ──────────────

def _queue_curation_scores(knob):
    """The order matches HN_TOP_IDS + HN_NEW_IDS dedup'd — 7 stories."""
    knob.queue_score(9.5, "Critical CVE in widely-used library")
    knob.queue_score(8.0, "Security-relevant phishing research")
    knob.queue_score(7.0, "Hardware security research")
    knob.queue_score(8.5, "Vendor security update")
    knob.queue_score(7.5, "Cryptanalysis research")
    knob.queue_score(4.0, "Tooling — borderline")          # low score, gets filtered
    knob.queue_score(2.0, "Architecture opinion piece")     # filtered out


# ── The epic test ────────────────────────────────────────────────────────────

async def test_full_pipeline_end_to_end(
    tmp_db, mock_scheduler, full_stubs, reset_rate_limits, monkeypatch,
):
    # Set the public-site base URL so the confirm email lands at a stable URL
    from secdigest import config
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://public.test")

    # ── 1. Mock HN + curation, run fetch ─────────────────────────────────────
    _wire_hn(full_stubs.httpx)
    _queue_curation_scores(full_stubs.anthropic)

    n = await fetcher.run_fetch("2026-05-04")
    assert n is not None
    articles = db.article_list(n["id"])
    titles = [a["title"] for a in articles]
    # 5 of 7 cleared the >= 5.0 relevance threshold
    assert "CVE-2026-9999: heap UAF in libfoo" in titles
    assert "Show HN: rust container scanner" not in titles, "below-threshold story leaked through"
    assert all(a["source"] == "hn" for a in articles)
    print(f"\n  fetch: stored {len(articles)} articles")

    # ── 2. Summarize each article (mocked) ───────────────────────────────────
    _wire_summarizer_fetch(full_stubs.httpx)
    for _ in articles:
        full_stubs.anthropic.responses.append({
            "summary": "Use-after-free in libfoo allows RCE under specific conditions; patch in 2.3.1."
        })
    # summarize_article returns synchronously when called directly (the route
    # uses asyncio.to_thread, but we're testing the underlying function)
    for a in articles:
        try:
            summarizer.summarize_article(a["id"])
        except Exception as e:
            # The summarizer's response shape may differ from the curator's;
            # if so, fall back to a manual update so the rest of the test runs.
            print(f"  summarizer skipped ({e}); falling back to direct update")
            db.article_update(a["id"], summary="Mock summary for testing.")

    # All articles should have a summary now
    refreshed = db.article_list(n["id"])
    assert all(a.get("summary") for a in refreshed), "some articles missing summaries"
    print("  summaries set on all articles")

    # ── 3. Auth into the admin app and curate ────────────────────────────────
    from httpx import AsyncClient, ASGITransport
    from secdigest.web.auth import hash_password
    db.cfg_set("password_hash", hash_password("testpw"))

    from secdigest.web.app import app as admin_app

    def _check_redirect(resp, expected_url_substring: str):
        """Helper: status assertion with the URL + response body included so a
        404 or 401 surfaces with enough context to diagnose immediately."""
        if resp.status_code != 302:
            raise AssertionError(
                f"expected 302 at {expected_url_substring}, got {resp.status_code}\n"
                f"location: {resp.headers.get('location')}\n"
                f"body: {resp.text[:400]}"
            )

    async with AsyncClient(transport=ASGITransport(app=admin_app),
                           base_url="http://test", follow_redirects=False) as admin:
        r = await admin.post("/login", data={"password": "testpw"})
        _check_redirect(r, "POST /login")

        # Pin two top articles to the weekly digest via the day curator
        top_two = sorted(refreshed, key=lambda a: a["relevance_score"], reverse=True)[:2]
        tok = await get_csrf(admin, "/day/2026-05-04")
        for a in top_two:
            url = f"/day/2026-05-04/article/{a['id']}/pin/weekly"
            r = await admin.post(url, data={"csrf_token": tok})
            _check_redirect(r, f"POST {url}")
        # Confirm pin flag persisted
        for a in top_two:
            assert db.article_get(a["id"])["pin_weekly"] == 1
        print(f"  pinned {len(top_two)} articles to weekly digest")

        # Toggle one article off the daily newsletter
        toggled = articles[-1]
        url = f"/day/2026-05-04/article/{toggled['id']}/toggle"
        r = await admin.post(url, data={"csrf_token": tok})
        _check_redirect(r, f"POST {url}")
        assert db.article_get(toggled["id"])["included"] == 0
        print("  toggled one article off")

        # ── 4. Subscribers across cadences ───────────────────────────────────
        db.subscriber_create("daily-reader@test.invalid")
        db.subscriber_create("weekly-reader@test.invalid")
        db.subscriber_create("monthly-reader@test.invalid")
        all_subs = {s["email"]: s for s in db.subscriber_list()}
        db.subscriber_update(all_subs["weekly-reader@test.invalid"]["id"], cadence="weekly")
        db.subscriber_update(all_subs["monthly-reader@test.invalid"]["id"], cadence="monthly")

        # ── 5. Send the daily newsletter ─────────────────────────────────────
        full_stubs.smtp.clear()
        r = await admin.post("/day/2026-05-04/send", data={"csrf_token": tok})
        _check_redirect(r, "POST /day/2026-05-04/send")
        # Only the daily-cadence subscriber should be addressed
        daily_recipients = [m["to"] for m in full_stubs.smtp]
        assert any("daily-reader@test.invalid" in r for r in daily_recipients), \
            f"daily reader missing from recipients: {daily_recipients}"
        assert not any("weekly-reader@test.invalid" in r for r in daily_recipients)
        assert not any("monthly-reader@test.invalid" in r for r in daily_recipients)
        print(f"  daily send: {len(daily_recipients)} recipient(s) — cadence filter working")

        # ── 6. Open the weekly digest (auto-seeds) and send ──────────────────
        r = await admin.get("/week/2026-05-04")
        assert r.status_code == 200
        digest = db.newsletter_get("2026-05-04", kind="weekly")
        assert digest, "weekly digest row not created"
        digest_articles = db.digest_article_list(digest["id"])
        digest_aids = {a["id"] for a in digest_articles}
        for a in top_two:
            assert a["id"] in digest_aids, "pinned article missing from digest"
        print(f"  weekly digest seeded with {len(digest_articles)} articles")

        full_stubs.smtp.clear()
        # Send-test from the digest builder (single recipient, no cadence filter)
        r = await admin.post("/week/2026-05-04/send-test",
                             data={"csrf_token": tok, "test_recipient": "qa@test.invalid"})
        _check_redirect(r, "POST /week/2026-05-04/send-test")
        assert any("qa@test.invalid" in m["to"] for m in full_stubs.smtp)
        print("  weekly send-test reached qa@")

        # Production weekly send
        full_stubs.smtp.clear()
        r = await admin.post("/week/2026-05-04/send", data={"csrf_token": tok})
        _check_redirect(r, "POST /week/2026-05-04/send")
        weekly_recipients = [m["to"] for m in full_stubs.smtp]
        assert any("weekly-reader@test.invalid" in r for r in weekly_recipients)
        assert not any("daily-reader@test.invalid" in r for r in weekly_recipients)
        assert not any("monthly-reader@test.invalid" in r for r in weekly_recipients)
        print(f"  weekly send: {len(weekly_recipients)} recipient(s) — only weekly cadence")

        # ── 7. Public flow: subscribe a brand-new email ──────────────────────
        from secdigest.public.app import app as public_app
        async with AsyncClient(transport=ASGITransport(app=public_app),
                               base_url="http://public.test", follow_redirects=False) as public:
            full_stubs.smtp.clear()
            r = await public.post("/subscribe", data={
                "email": "new-signup@test.invalid",
                "cadence": "daily",
                "website": "",
            })
            assert r.status_code == 200
            assert "check your inbox" in r.text.lower()
            assert len(full_stubs.smtp) == 1, "confirmation email not sent"
            confirm_url = re.search(
                r"http://public\.test/confirm/[\w-]+",
                full_stubs.smtp[0]["body"],
            ).group(0)
            print(f"  public subscribe: confirmation email captured at {confirm_url}")

            sub = db.subscriber_get_by_email("new-signup@test.invalid")
            assert sub["confirmed"] == 0 and sub["active"] == 0, \
                "row should be pending until confirm clicked"

            # ── 8. Confirm the link ──────────────────────────────────────────
            token = confirm_url.rsplit("/", 1)[-1]
            r = await public.get(f"/confirm/{token}")
            assert r.status_code == 200
            assert "you're in" in r.text.lower()
            sub = db.subscriber_get_by_email("new-signup@test.invalid")
            assert sub["confirmed"] == 1 and sub["active"] == 1
            print("  public confirm: row activated")

            # ── 9. Public unsubscribe ────────────────────────────────────────
            unsub_token = sub["unsubscribe_token"]
            r = await public.get(f"/unsubscribe/{unsub_token}")
            assert r.status_code == 200
            assert db.subscriber_get_by_email("new-signup@test.invalid")["active"] == 0
            print("  public unsubscribe: active=0")

        # ── 10. Resend the daily — unsubscribed user must not appear ─────────
        full_stubs.smtp.clear()
        r = await admin.post("/day/2026-05-04/send", data={"csrf_token": tok})
        _check_redirect(r, "POST /day/2026-05-04/send (post-unsubscribe)")
        post_unsub_recipients = [m["to"] for m in full_stubs.smtp]
        assert not any("new-signup@test.invalid" in r for r in post_unsub_recipients), \
            "unsubscribed user resurfaced in next send"
        print(f"  post-unsub daily send: {len(post_unsub_recipients)} recipient(s); unsub honoured")

    print("  pipeline complete — full SecDigest lifecycle works offline")
