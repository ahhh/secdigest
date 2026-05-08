"""Fetch-summary banner: persist the latest run_fetch outcome to config_kv
and surface it on the matching day-curator page so the operator can see
how many HN + RSS articles were just pulled and how many got stored.

Test coverage:

  • _format_fetch_summary — every branch (empty feeds, dedup ate everything,
    below-threshold scoring, normal storage)
  • _record_fetch_summary — writes message + date to config_kv
  • run_fetch end-to-end — picks the right branch on each pipeline path
    AND skips the write on the early-return ('articles already fetched')
    path so a fresh summary isn't clobbered by a no-op re-click
  • day_view route — banner shows for the matching date only, hides
    everywhere else
  • dismiss endpoint — clears both keys, redirects back to the day
"""
import pytest

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
    The seed lives on a different day so the run_fetch target date doesn't
    short-circuit on the 'already has articles' path."""
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


async def test_run_fetch_skip_path_preserves_prior_summary(
        tmp_db, full_stubs):
    """When the day already has articles, run_fetch returns early without
    pulling. The user gets 'Articles already fetched' via ?msg= for that
    case; we must NOT clobber the earlier real summary with a fake zero."""
    db.cfg_set("last_fetch_summary",
               "Pulled 5 HN + 2 RSS → 3 stored, 3 included")
    db.cfg_set("last_fetch_summary_date", "2026-05-08")

    n = db.newsletter_get_or_create("2026-05-08")
    db.article_insert(
        newsletter_id=n["id"], hn_id=1, title="t",
        url="https://example.invalid/x",
        hn_score=0, hn_comments=0,
        relevance_score=8.0, relevance_reason="r", position=0,
    )

    await fetcher.run_fetch("2026-05-08")
    # Untouched — no 'feeds returned nothing' clobber on the skip path.
    assert db.cfg_get("last_fetch_summary") == \
        "Pulled 5 HN + 2 RSS → 3 stored, 3 included"
    assert db.cfg_get("last_fetch_summary_date") == "2026-05-08"


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
