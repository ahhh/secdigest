"""Manual-add flow on the day curator (`/day/{date}/article/add`).

Pins three behaviours:

1. A typed-in summary is saved verbatim — auto-generate must NOT run on top of it.
2. With `auto_summarize=1` AND a blank summary, the summarizer is invoked
   exactly once for the new article. This is the bug-fix anchor: the route
   previously used `asyncio.create_task(...)` without holding a strong
   reference, so Python 3.11+ would GC the task and the click silently
   produced no summary. We assert the call lands.
3. With the checkbox unchecked (or omitted) and a blank summary, no summary
   is generated and no summarizer call is made.

Plus the URL/summary required guard, so the form's two-required-fields rule
doesn't regress.
"""
import pytest

from secdigest import db
from tests.conftest import get_csrf


@pytest.fixture
def seeded_day(tmp_db):
    """Create a daily newsletter row so /day/{date}/article/add has a target."""
    db.newsletter_get_or_create("2026-05-04")
    return "2026-05-04"


@pytest.fixture
def summarize_spy(monkeypatch):
    """Replace summarizer.summarize_article with a recorder.

    The route imports summarizer as a module (`from secdigest import ... summarizer`)
    and calls `summarizer.summarize_article` via attribute access at task-fire
    time, so patching the module attribute is sufficient — the BackgroundTask
    resolves through the same reference when Starlette runs it.
    """
    calls: list[int] = []

    def _spy(article_id: int) -> str:
        calls.append(article_id)
        # Simulate the success path so `db.article_update` would be exercised
        # if a future change starts asserting summary content. We keep it
        # cheap because none of these tests look at the summary text.
        db.article_update(article_id, summary="spy-stub-summary")
        return "spy-stub-summary"

    from secdigest import summarizer
    monkeypatch.setattr(summarizer, "summarize_article", _spy)
    return calls


async def _post_add(client, date_str: str, **fields):
    """POST the add-article form. Returns (response, new article row or None).

    The route 302s back to /day/{date}; we don't follow the redirect because
    the test wants to inspect the response code itself.
    """
    tok = await get_csrf(client, f"/day/{date_str}")
    payload = {"csrf_token": tok, **fields}
    return await client.post(f"/day/{date_str}/article/add", data=payload)


def _latest_article(date_str: str) -> dict:
    n = db.newsletter_get(date_str)
    rows = db.article_list(n["id"])
    assert rows, "expected at least one article seeded by the POST"
    # article_list orders by position ASC; the just-added row is the highest-positioned
    return max(rows, key=lambda a: a["position"])


# ── 1. Typed summary wins, auto-generate never fires ─────────────────────────

async def test_add_article_with_typed_summary_saves_verbatim(
        admin_client, seeded_day, summarize_spy):
    """When the operator types a summary, it lands in the DB exactly as typed
    and the auto-generate background task is never scheduled — even if the
    checkbox is checked. Typed text is the source of truth."""
    r = await _post_add(
        admin_client, seeded_day,
        url="https://example.invalid/post",
        title="Hand-written entry",
        summary="A precise, operator-authored summary.",
        auto_summarize="1",  # checkbox checked — but the summary field wins
    )
    assert r.status_code == 302

    a = _latest_article(seeded_day)
    assert a["title"] == "Hand-written entry"
    assert a["summary"] == "A precise, operator-authored summary."
    assert summarize_spy == [], (
        "summarizer must not run when the operator supplied their own summary"
    )


# ── 2. Blank + checkbox → summarizer is invoked (the bug fix) ────────────────

async def test_add_article_blank_summary_with_auto_generate_runs_summarizer(
        admin_client, seeded_day, summarize_spy):
    """The headline regression test for the create_task → BackgroundTasks
    migration. Previously the auto-generate task could be GC'd before it ran;
    now Starlette holds it until the response cycle finishes, so the spy
    must record exactly one call for the just-added article."""
    r = await _post_add(
        admin_client, seeded_day,
        url="https://example.invalid/auto",
        title="Needs auto-summary",
        summary="",
        auto_summarize="1",
    )
    assert r.status_code == 302

    a = _latest_article(seeded_day)
    assert summarize_spy == [a["id"]], (
        f"summarize_article should have been called once for article {a['id']}, "
        f"got calls: {summarize_spy}"
    )
    # And the spy persisted its summary, proving the BG task ran end-to-end
    assert a["summary"] == "spy-stub-summary"


# ── 3. Blank + checkbox unchecked → nothing happens ──────────────────────────

async def test_add_article_blank_summary_no_auto_generate_leaves_summary_empty(
        admin_client, seeded_day, summarize_spy):
    """Checkbox unchecked means the form omits `auto_summarize` entirely; the
    route's `Form("0")` default kicks in. No summarizer call, no summary."""
    r = await _post_add(
        admin_client, seeded_day,
        url="https://example.invalid/no-auto",
        title="Stays summary-less",
        summary="",
        # auto_summarize omitted on purpose — that's how an unchecked checkbox
        # behaves in real form submissions
    )
    assert r.status_code == 302

    a = _latest_article(seeded_day)
    assert summarize_spy == [], (
        "summarizer must not run when the auto-generate box is unchecked"
    )
    assert (a["summary"] or "") == ""


async def test_add_article_blank_summary_explicit_zero_skips_summarize(
        admin_client, seeded_day, summarize_spy):
    """Belt-and-braces: even if a client explicitly POSTs `auto_summarize=0`
    (e.g. a custom integration), the route must treat it as off."""
    r = await _post_add(
        admin_client, seeded_day,
        url="https://example.invalid/zero",
        title="Explicit-zero auto flag",
        summary="",
        auto_summarize="0",
    )
    assert r.status_code == 302
    assert summarize_spy == []


# ── 4. URL-or-summary required guard ─────────────────────────────────────────

async def test_add_article_requires_url_or_summary(
        admin_client, seeded_day, summarize_spy):
    """An editorial-note row needs at least a summary; a real article needs
    at least a URL. Empty + empty must be rejected with the error redirect,
    no row inserted, no summarizer scheduled."""
    n_before = len(db.article_list(db.newsletter_get(seeded_day)["id"]))

    r = await _post_add(
        admin_client, seeded_day,
        url="",
        title="Nothing to anchor on",
        summary="",
        auto_summarize="1",
    )
    assert r.status_code == 302
    assert "status=error" in r.headers.get("location", "")

    n_after = len(db.article_list(db.newsletter_get(seeded_day)["id"]))
    assert n_after == n_before, "no article should be created on the error path"
    assert summarize_spy == []


# ── 5. Editorial note (no URL) with typed summary still works ────────────────

async def test_add_editorial_note_with_summary_only(
        admin_client, seeded_day, summarize_spy):
    """An editorial note has no URL but does have a summary. Auto-generate
    is irrelevant here (typed summary wins) — but the row must be tagged
    'editorial note' in `relevance_reason` so it's distinguishable later."""
    r = await _post_add(
        admin_client, seeded_day,
        url="",
        title="Editor's commentary",
        summary="Inline note — no source link.",
        auto_summarize="1",
    )
    assert r.status_code == 302

    a = _latest_article(seeded_day)
    assert a["url"] == ""
    assert a["summary"] == "Inline note — no source link."
    assert a["relevance_reason"] == "editorial note"
    assert summarize_spy == []
