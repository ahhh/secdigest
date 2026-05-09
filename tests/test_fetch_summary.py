"""Fetch-summary banner: persist the latest run_fetch outcome to config_kv
and surface it on the matching day-curator page so the operator can see
how many HN + RSS articles were just pulled and how many got stored.

Test coverage:

  • _format_fetch_summary — every branch (empty feeds, dedup ate everything,
    below-threshold scoring, pool-full, normal storage)
  • _record_fetch_summary — writes message + date to config_kv
  • run_fetch end-to-end — picks the right branch on each pipeline path
    AND re-fetches APPEND unique articles to the pool (no early-return
    skip when a day already has articles)
  • day_view route — banner shows for the matching date only, hides
    everywhere else
  • dismiss endpoint — clears both keys, redirects back to the day
"""
import asyncio

from secdigest import db, fetcher
from secdigest.fetcher import _format_fetch_summary, _record_fetch_summary
from tests.conftest import get_csrf


# ── _format_fetch_summary: one test per branch ───────────────────────────────

def test_format_fetch_summary_zero_zero_says_feeds_returned_nothing():
    """Both feeds returned 0 candidates — usually means HN_MIN_SCORE is too
    high or active RSS feeds are dead. Distinct from 'all already seen'."""
    assert _format_fetch_summary(hn=0, rss=0, new_count=0,
                                 stored=0, included=0) == \
        "Pulled 0 HN + 0 RSS — feeds returned nothing"


def test_format_fetch_summary_dedup_ate_everything():
    """Candidates exist but every URL was already in our DB. Common when
    re-pulling a day that's already been fetched (manual fetch on a date
    with prior coverage)."""
    msg = _format_fetch_summary(hn=12, rss=5, new_count=0,
                                stored=0, included=0)
    assert msg == "Pulled 12 HN + 5 RSS → 0 new (all already seen)"


def test_format_fetch_summary_below_relevance_threshold():
    """Articles cleared dedup but Claude scored every one < 5.0 — the
    operator may need to loosen the curation prompt."""
    msg = _format_fetch_summary(hn=8, rss=3, new_count=11,
                                stored=0, included=0)
    assert "below relevance threshold" in msg
    assert "11 new" in msg


def test_format_fetch_summary_pool_full_branch_is_distinct():
    """When stored=0 because the day's pool is at max_articles, the message
    must NOT say 'below relevance threshold' (operator action differs)."""
    msg = _format_fetch_summary(hn=8, rss=3, new_count=11,
                                stored=0, included=0, pool_full=True)
    assert "pool already at max_articles cap" in msg
    assert "below relevance threshold" not in msg
    assert "11 new" in msg


def test_format_fetch_summary_normal_path_includes_counts():
    msg = _format_fetch_summary(hn=12, rss=5, new_count=15,
                                stored=8, included=8)
    assert msg == "Pulled 12 HN + 5 RSS → 8 stored, 8 included"


def test_format_fetch_summary_partial_inclusion():
    """When stored > max_curator, included < stored — the banner should
    show both numbers so the operator can see how much spilled to the pool."""
    msg = _format_fetch_summary(hn=20, rss=0, new_count=20,
                                stored=15, included=10)
    assert msg == "Pulled 20 HN + 0 RSS → 15 stored, 10 included"


# ── _record_fetch_summary persists both keys ────────────────────────────────

def test_record_fetch_summary_writes_message_and_date(tmp_db):
    """The date field is what lets the day route filter the banner to only
    the page that was actually fetched. Both keys must land together."""
    _record_fetch_summary("2026-05-08", hn=10, rss=3, new_count=12,
                          stored=8, included=6)
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"
    msg = db.cfg_get("last_fetch_summary")
    assert "Pulled 10 HN + 3 RSS" in msg
    assert "8 stored, 6 included" in msg


def test_record_fetch_summary_overwrites_prior_record(tmp_db):
    """A new fetch always supersedes an older one — there's only ever one
    'last' summary."""
    _record_fetch_summary("2026-05-07", hn=5, rss=0, new_count=5,
                          stored=3, included=3)
    _record_fetch_summary("2026-05-08", hn=12, rss=5, new_count=15,
                          stored=8, included=8)
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"
    assert "12 HN + 5 RSS" in db.cfg_get("last_fetch_summary")


# ── run_fetch end-to-end via the existing stub fixtures ──────────────────────

async def test_run_fetch_records_summary_on_normal_storage_path(
        tmp_db, full_stubs):
    """Two HN stories, both score above 5.0 — both end up stored, summary
    matches."""
    full_stubs.httpx.route("topstories.json", json_data=[2001, 2002])
    full_stubs.httpx.route("newstories.json", json_data=[])
    full_stubs.httpx.route(
        "/item/2001.json",
        json_data={"id": 2001, "type": "story", "title": "CVE-2026-1: heap UAF",
                   "url": "https://example.invalid/cve-1",
                   "score": 200, "descendants": 12},
    )
    full_stubs.httpx.route(
        "/item/2002.json",
        json_data={"id": 2002, "type": "story", "title": "Critical RCE in widget",
                   "url": "https://example.invalid/widget",
                   "score": 150, "descendants": 8},
    )
    full_stubs.anthropic.queue_score(9.0, "critical CVE")
    full_stubs.anthropic.queue_score(8.5, "RCE")

    await fetcher.run_fetch("2026-05-08")
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"
    msg = db.cfg_get("last_fetch_summary")
    assert msg.startswith("Pulled 2 HN + 0 RSS")
    assert "2 stored" in msg


async def test_run_fetch_records_empty_summary_when_feeds_return_nothing(
        tmp_db, full_stubs):
    """HN topstories+newstories both empty; no active RSS feeds in tmp_db.
    Summary should reflect the 'feeds returned nothing' branch — actionable
    signal that nothing was actually pulled."""
    full_stubs.httpx.route("topstories.json", json_data=[])
    full_stubs.httpx.route("newstories.json", json_data=[])

    await fetcher.run_fetch("2026-05-08")
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"
    assert db.cfg_get("last_fetch_summary") == \
        "Pulled 0 HN + 0 RSS — feeds returned nothing"


async def test_run_fetch_records_dedup_summary_when_all_urls_seen(
        tmp_db, full_stubs):
    """Insert an article whose URL we'll then re-pull — dedup drops it,
    new_stories ends up empty, summary picks the 'all already seen' branch.
    Seed lives on a different day so dedup works through `article_all_urls()`
    rather than the same-day pool snapshot."""
    n = db.newsletter_get_or_create("2026-05-07")
    db.article_insert(
        newsletter_id=n["id"], hn_id=9001, title="seen before",
        url="https://example.invalid/dup",
        hn_score=100, hn_comments=0,
        relevance_score=8.0, relevance_reason="seed", position=0,
    )

    full_stubs.httpx.route("topstories.json", json_data=[9001])
    full_stubs.httpx.route("newstories.json", json_data=[])
    full_stubs.httpx.route(
        "/item/9001.json",
        json_data={"id": 9001, "type": "story", "title": "seen before",
                   "url": "https://example.invalid/dup",
                   "score": 100, "descendants": 0},
    )

    await fetcher.run_fetch("2026-05-08")
    assert db.cfg_get("last_fetch_summary") == \
        "Pulled 1 HN + 0 RSS → 0 new (all already seen)"


async def test_run_fetch_appends_unique_articles_to_existing_pool(
        tmp_db, full_stubs):
    """Re-fetching a day that already has articles must NOT short-circuit;
    it should pull, dedup, and append unique candidates to the existing pool.
    The seed article and one HN candidate share a URL, so dedup drops the
    duplicate; the second HN candidate is fresh and must land at the next
    free position."""
    n = db.newsletter_get_or_create("2026-05-08")
    db.article_insert(
        newsletter_id=n["id"], hn_id=8001, title="seeded earlier",
        url="https://example.invalid/dup",
        hn_score=100, hn_comments=0,
        relevance_score=8.0, relevance_reason="seed", position=0,
        included=1,
    )

    full_stubs.httpx.route("topstories.json", json_data=[8001, 8002])
    full_stubs.httpx.route("newstories.json", json_data=[])
    full_stubs.httpx.route(
        "/item/8001.json",
        json_data={"id": 8001, "type": "story", "title": "seeded earlier",
                   "url": "https://example.invalid/dup",
                   "score": 100, "descendants": 0},
    )
    full_stubs.httpx.route(
        "/item/8002.json",
        json_data={"id": 8002, "type": "story", "title": "freshly available",
                   "url": "https://example.invalid/fresh",
                   "score": 200, "descendants": 5},
    )
    full_stubs.anthropic.queue_score(8.5, "fresh CVE")  # only one new story to score

    await fetcher.run_fetch("2026-05-08")

    rows = db.article_list(n["id"])
    assert len(rows) == 2, "re-fetch should append the fresh URL"
    titles = {r["title"] for r in rows}
    assert titles == {"seeded earlier", "freshly available"}

    # Position continues from the existing pool, not restarting at 0
    fresh = next(r for r in rows if r["title"] == "freshly available")
    assert fresh["position"] == 1, (
        f"appended article must take the next position; got {fresh['position']}"
    )

    # Summary reflects the *new* batch (1 stored), not the cumulative pool
    msg = db.cfg_get("last_fetch_summary")
    assert "1 stored" in msg


async def test_run_fetch_re_pull_with_no_new_uniques_records_dedup_summary(
        tmp_db, full_stubs):
    """Re-fetch where every candidate URL is already known — dedup eats
    everything, the pool stays the same size, and the banner explains why."""
    n = db.newsletter_get_or_create("2026-05-08")
    db.article_insert(
        newsletter_id=n["id"], hn_id=7001, title="already here",
        url="https://example.invalid/known",
        hn_score=100, hn_comments=0,
        relevance_score=7.0, relevance_reason="seed", position=0,
    )

    full_stubs.httpx.route("topstories.json", json_data=[7001])
    full_stubs.httpx.route("newstories.json", json_data=[])
    full_stubs.httpx.route(
        "/item/7001.json",
        json_data={"id": 7001, "type": "story", "title": "already here",
                   "url": "https://example.invalid/known",
                   "score": 100, "descendants": 0},
    )

    await fetcher.run_fetch("2026-05-08")
    assert len(db.article_list(n["id"])) == 1, "no duplicates appended"
    assert db.cfg_get("last_fetch_summary") == \
        "Pulled 1 HN + 0 RSS → 0 new (all already seen)"


async def test_run_fetch_pool_full_pre_check_skips_network(
        tmp_db, full_stubs):
    """Pool is at max_articles before the fetch starts — the pre-check in
    run_fetch must short-circuit before any HTTP. Past incident: a fetch
    on a slow HN day waited ~3 minutes only to discover there was no room.
    Avoids that round-trip when we already know the answer."""
    db.cfg_set("max_articles", "2")  # tight cap so the test stays compact

    n = db.newsletter_get_or_create("2026-05-08")
    for i, slug in enumerate(["a", "b"]):
        db.article_insert(
            newsletter_id=n["id"], hn_id=6000 + i,
            title=f"existing-{slug}",
            url=f"https://example.invalid/{slug}",
            hn_score=100, hn_comments=0,
            relevance_score=8.0, relevance_reason="seed", position=i,
        )

    # Deliberately leave the httpx routes empty — if the pre-check fires,
    # nothing should reach the stub.
    await fetcher.run_fetch("2026-05-08")

    assert len(db.article_list(n["id"])) == 2, "pool cap held — no appends"
    msg = db.cfg_get("last_fetch_summary")
    assert "skipped fetch" in msg
    assert "2/2" in msg, f"summary should show pool cap progress; got: {msg!r}"
    assert full_stubs.httpx.calls == [], (
        f"pre-check should skip all HTTP; httpx was called: "
        f"{full_stubs.httpx.calls!r}"
    )


async def test_run_fetch_does_not_double_include_when_curator_already_full(
        tmp_db, full_stubs):
    """If max_curator slots are already taken by existing articles, newly
    appended articles must default to included=0 (pool only) so they don't
    silently push the email send beyond the operator's curator cap."""
    db.cfg_set("max_curator_articles", "2")
    db.cfg_set("max_articles", "10")

    n = db.newsletter_get_or_create("2026-05-08")
    for i, slug in enumerate(["a", "b"]):
        db.article_insert(
            newsletter_id=n["id"], hn_id=5000 + i,
            title=f"existing-{slug}",
            url=f"https://example.invalid/seed-{slug}",
            hn_score=100, hn_comments=0,
            relevance_score=8.0, relevance_reason="seed", position=i,
            included=1,
        )

    full_stubs.httpx.route("topstories.json", json_data=[5100])
    full_stubs.httpx.route("newstories.json", json_data=[])
    full_stubs.httpx.route(
        "/item/5100.json",
        json_data={"id": 5100, "type": "story", "title": "fresh-ext",
                   "url": "https://example.invalid/seed-ext",
                   "score": 100, "descendants": 0},
    )
    full_stubs.anthropic.queue_score(9.0, "newly relevant")

    await fetcher.run_fetch("2026-05-08")
    rows = db.article_list(n["id"])
    fresh = next(r for r in rows if r["title"] == "fresh-ext")
    assert fresh["included"] == 0, (
        "curator cap was already met by the existing pool; "
        "appended article must land in the pool, not auto-include"
    )

    # The summary's `included` count reflects the new batch only — and is 0.
    msg = db.cfg_get("last_fetch_summary")
    assert "1 stored, 0 included" in msg


async def test_run_fetch_wall_clock_timeout_records_summary(
        tmp_db, full_stubs, monkeypatch):
    """Outer wall-clock guard prevents a slow HN day from pinning the worker
    indefinitely. Past incident: a 3-minute hang during the fetch led to a
    manual systemd stop. With the guard, the pipeline aborts at the cap and
    the banner explains what happened — re-clicking is safe because the
    URL-level dedup is idempotent."""
    # Tighten the wall-clock to keep the test sub-second.
    monkeypatch.setattr(fetcher, "_RUN_FETCH_WALLCLOCK_SECONDS", 0.05)

    async def _hangs():
        # Sleep longer than the test timeout — the inner pipeline never
        # gets past this point, simulating an HN/RSS endpoint that's stuck.
        await asyncio.sleep(5.0)
        return []

    monkeypatch.setattr(fetcher, "fetch_all_candidates", _hangs)

    await fetcher.run_fetch("2026-05-08")

    msg = db.cfg_get("last_fetch_summary")
    assert "timed out" in msg, f"expected timeout banner; got: {msg!r}"
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"


async def test_run_fetch_wall_clock_timeout_does_not_partial_commit_summary(
        tmp_db, full_stubs, monkeypatch):
    """When the inner pipeline gets cancelled by the wall-clock guard, the
    *outer* timeout summary must win — the inner half-state must not leak
    a misleading 'X stored' banner to the operator."""
    monkeypatch.setattr(fetcher, "_RUN_FETCH_WALLCLOCK_SECONDS", 0.05)

    async def _hangs():
        await asyncio.sleep(5.0)
        return []

    monkeypatch.setattr(fetcher, "fetch_all_candidates", _hangs)

    # Pre-seed a stale summary from a hypothetical earlier successful fetch.
    db.cfg_set("last_fetch_summary",
               "Pulled 5 HN + 0 RSS → 5 stored, 5 included")
    db.cfg_set("last_fetch_summary_date", "2026-05-07")

    await fetcher.run_fetch("2026-05-08")

    msg = db.cfg_get("last_fetch_summary")
    assert "timed out" in msg
    # Date must be the timed-out fetch's date, not the prior successful one.
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"


async def test_run_fetch_appends_position_continues_across_multiple_runs(
        tmp_db, full_stubs):
    """Two consecutive re-fetches: positions must keep marching forward
    so the curator's drag-to-reorder list stays stable."""
    n = db.newsletter_get_or_create("2026-05-08")

    # First fetch: one fresh URL
    full_stubs.httpx.route("topstories.json", json_data=[4001])
    full_stubs.httpx.route("newstories.json", json_data=[])
    full_stubs.httpx.route(
        "/item/4001.json",
        json_data={"id": 4001, "type": "story", "title": "first",
                   "url": "https://example.invalid/first",
                   "score": 100, "descendants": 0},
    )
    full_stubs.anthropic.queue_score(9.0, "r1")
    await fetcher.run_fetch("2026-05-08")

    # Second fetch: a different fresh URL, the first is now in historical_urls
    full_stubs.httpx.route("topstories.json", json_data=[4002])
    full_stubs.httpx.route(
        "/item/4002.json",
        json_data={"id": 4002, "type": "story", "title": "second",
                   "url": "https://example.invalid/second",
                   "score": 100, "descendants": 0},
    )
    full_stubs.anthropic.queue_score(9.0, "r2")
    await fetcher.run_fetch("2026-05-08")

    rows = sorted(db.article_list(n["id"]), key=lambda r: r["position"])
    assert [r["title"] for r in rows] == ["first", "second"]
    assert [r["position"] for r in rows] == [0, 1]


# ── day_view banner rendering ────────────────────────────────────────────────

async def test_day_view_renders_banner_when_summary_date_matches(
        admin_client):
    db.newsletter_get_or_create("2026-05-08")
    db.cfg_set("last_fetch_summary",
               "Pulled 12 HN + 5 RSS → 8 stored, 8 included")
    db.cfg_set("last_fetch_summary_date", "2026-05-08")

    r = await admin_client.get("/day/2026-05-08")
    assert r.status_code == 200
    # The literal arrow may pass through Jinja autoescape unchanged (it's
    # neither <, >, &, nor a quote). Either form is fine for the banner.
    assert ("Pulled 12 HN + 5 RSS → 8 stored, 8 included" in r.text or
            "Pulled 12 HN + 5 RSS &#x2192; 8 stored, 8 included" in r.text)


async def test_day_view_hides_banner_for_other_dates(admin_client):
    """Banner is keyed to the date that was *fetched*, not the day being
    viewed. After a fetch on 2026-05-08, navigating to 2026-05-09 must
    NOT show the previous day's numbers."""
    db.newsletter_get_or_create("2026-05-09")
    db.cfg_set("last_fetch_summary",
               "Pulled 12 HN + 5 RSS → 8 stored, 8 included")
    db.cfg_set("last_fetch_summary_date", "2026-05-08")

    r = await admin_client.get("/day/2026-05-09")
    assert r.status_code == 200
    assert "Pulled 12 HN" not in r.text


async def test_day_view_no_banner_when_summary_blank(admin_client):
    """Fresh DB with no fetch summary recorded yet — banner must not
    render at all (no empty banner box)."""
    db.newsletter_get_or_create("2026-05-08")
    # Don't set the keys — they default to "" via cfg_get's fallback.

    r = await admin_client.get("/day/2026-05-08")
    assert r.status_code == 200
    assert "Pulled" not in r.text
    assert "dismiss-fetch-summary" not in r.text


# ── Dismiss endpoint clears both keys ────────────────────────────────────────

async def test_dismiss_fetch_summary_clears_both_keys(admin_client):
    db.cfg_set("last_fetch_summary",
               "Pulled 12 HN + 5 RSS → 8 stored, 8 included")
    db.cfg_set("last_fetch_summary_date", "2026-05-08")

    tok = await get_csrf(admin_client, "/day/2026-05-08")
    r = await admin_client.post(
        "/day/2026-05-08/dismiss-fetch-summary",
        data={"csrf_token": tok},
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/day/2026-05-08"
    assert db.cfg_get("last_fetch_summary") == ""
    assert db.cfg_get("last_fetch_summary_date") == ""


async def test_dismiss_fetch_summary_requires_auth(tmp_db, mock_scheduler):
    """The dismiss POST is a state-changing route — no session, no clear."""
    from httpx import AsyncClient, ASGITransport
    from secdigest.web.app import app

    db.cfg_set("last_fetch_summary",
               "Pulled 12 HN + 5 RSS → 8 stored, 8 included")
    db.cfg_set("last_fetch_summary_date", "2026-05-08")

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", follow_redirects=False) as c:
        # No CSRF token + no session — the verify_csrf dep + auth check
        # both gate the route. We don't care which one bounces; just that
        # the keys aren't touched.
        r = await c.post("/day/2026-05-08/dismiss-fetch-summary")
    assert r.status_code in (302, 401, 403)
    # Untouched
    assert db.cfg_get("last_fetch_summary") == \
        "Pulled 12 HN + 5 RSS → 8 stored, 8 included"
