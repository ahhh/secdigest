"""Strong-reference task helper used by the day-curator routes.

Background: `asyncio.create_task` returns a Task that the event loop only
holds via a *weak* reference (Python 3.11+). If nothing else keeps a strong
reference, the GC can collect the Task before its coroutine finishes, and
the click silently produces nothing — exactly the symptom that bit the
fetch + summarize buttons.

`_BG_TASKS` plus `_spawn_bg` is the canonical fix: pin the Task in a
module-level set on schedule, drain it on completion via `add_done_callback`.

These tests pin three contracts:

  1. The set actually holds the Task while it's running (so GC can't collect).
  2. The done-callback removes finished tasks (no unbounded growth).
  3. The two long-pipeline routes (`day_fetch`, `day_summarize`) wire through
     `_spawn_bg` and not bare `asyncio.create_task` — a regression check that
     a future refactor doesn't slip back to the old pattern.
"""
import asyncio
import gc

import pytest

from secdigest.web.routes import newsletter as newsletter_routes
from tests.conftest import get_csrf


# ── 1. Set membership while running ──────────────────────────────────────────

async def test_spawn_bg_adds_running_task_to_module_set():
    """A spawned task must be in `_BG_TASKS` immediately — that's the strong
    reference that defeats the weak-ref GC hazard."""
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def _work():
        started.set()
        await proceed.wait()

    task = newsletter_routes._spawn_bg(_work())
    try:
        await started.wait()
        assert task in newsletter_routes._BG_TASKS, (
            "_spawn_bg must add the task to _BG_TASKS so GC can't collect it"
        )
    finally:
        proceed.set()
        await task


# ── 2. Set drains on completion ──────────────────────────────────────────────

async def test_spawn_bg_drains_completed_task_from_set():
    """The done-callback must remove a finished task; otherwise the set
    grows without bound under steady fetch/summarize traffic."""
    async def _work():
        return 42

    task = newsletter_routes._spawn_bg(_work())
    assert task in newsletter_routes._BG_TASKS
    await task
    # Yield once so the done-callback queued by asyncio runs.
    await asyncio.sleep(0)
    assert task not in newsletter_routes._BG_TASKS


async def test_spawn_bg_drains_set_even_when_task_raises():
    """Fire-and-forget contract: an exception inside the task must still
    let the done-callback fire so the set stays clean. We don't assert the
    exception is silenced — the route is responsible for that — only that
    the leak doesn't happen."""
    async def _boom():
        raise RuntimeError("kaboom")

    task = newsletter_routes._spawn_bg(_boom())
    with pytest.raises(RuntimeError, match="kaboom"):
        await task
    await asyncio.sleep(0)
    assert task not in newsletter_routes._BG_TASKS


# ── 3. The GC-survives test (the bug fix) ────────────────────────────────────

async def test_spawn_bg_survives_local_reference_drop_and_gc():
    """The headline regression: spawn a task, drop the local reference,
    force GC, and confirm the coroutine still runs to completion. Without
    `_BG_TASKS` this is the exact scenario where Python 3.11+ collected
    the orphan task and the click silently produced nothing."""
    completed = asyncio.Event()

    async def _work():
        # A real awaitable; gives GC a chance to run between the spawn and
        # the resumption.
        await asyncio.sleep(0.01)
        completed.set()

    # Deliberately don't capture the return value — this mimics the route
    # handler: spawn-and-forget, no local variable.
    newsletter_routes._spawn_bg(_work())
    gc.collect()

    # 1s ceiling so a regression doesn't hang the suite forever.
    await asyncio.wait_for(completed.wait(), timeout=1.0)


# ── 4. Routes wire through the helper, not bare create_task ──────────────────

async def test_day_fetch_route_uses_spawn_bg(admin_client, monkeypatch):
    """If a future change reverts `day_fetch` to bare `asyncio.create_task`,
    this test fails — the recorder never gets called."""
    spawned: list = []

    def _recorder(coro):
        spawned.append(coro)
        # Close the coroutine so we don't get an "never awaited" warning.
        coro.close()
        # Return a dummy completed task so the route can still wire it.
        async def _noop():
            return None
        return asyncio.ensure_future(_noop())

    monkeypatch.setattr(newsletter_routes, "_spawn_bg", _recorder)

    tok = await get_csrf(admin_client, "/day/2026-05-08")
    r = await admin_client.post(
        "/day/2026-05-08/fetch",
        data={"csrf_token": tok},
    )
    assert r.status_code == 302
    assert len(spawned) == 1, (
        f"day_fetch must route through _spawn_bg; got {len(spawned)} calls"
    )


async def test_day_summarize_route_uses_spawn_bg(admin_client, monkeypatch):
    """Same regression check for the summarize button."""
    from secdigest import db

    n = db.newsletter_get_or_create("2026-05-08")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="t",
        url="https://example.invalid/x", hn_score=0, hn_comments=0,
        relevance_score=8.0, relevance_reason="r", position=0,
    )

    spawned: list = []

    def _recorder(coro):
        spawned.append(coro)
        coro.close()
        async def _noop():
            return None
        return asyncio.ensure_future(_noop())

    monkeypatch.setattr(newsletter_routes, "_spawn_bg", _recorder)

    tok = await get_csrf(admin_client, "/day/2026-05-08")
    r = await admin_client.post(
        "/day/2026-05-08/summarize",
        data={"csrf_token": tok},
    )
    assert r.status_code == 302
    assert len(spawned) == 1, (
        f"day_summarize must route through _spawn_bg; got {len(spawned)} calls"
    )
