"""APScheduler daily job: fetch → summarize → optional auto-send.

This module wires up a single recurring job that runs once a day at the
configured ``fetch_time``. The pipeline mirrors what an admin would do
manually from the UI: pull new articles, ask Claude to summarise them,
and (if ``auto_send`` is enabled) email the resulting newsletter.

We use ``AsyncIOScheduler`` because FastAPI runs on the asyncio loop and
the fetcher already exposes async I/O — sharing the loop avoids spawning
a separate scheduler thread. A module-level ``_scheduler`` singleton is
fine here: only one scheduler should exist per process.
"""
import asyncio
from datetime import date as dt_date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from secdigest import db
from secdigest import fetcher, summarizer, mailer, areas

# Module-level singleton; populated by start_scheduler() and reused by
# reschedule()/stop_scheduler(). ``None`` means "not started yet".
_scheduler: AsyncIOScheduler | None = None


async def daily_job():
    """The cron-fired pipeline. Failures in any one stage are logged and
    the rest of the pipeline either bails out or carries on, depending on
    whether the next stage can do anything useful without it."""
    today = dt_date.today().isoformat()
    print(f"[scheduler] daily job for {today}")
    # Stage 1: fetch. If this fails we have nothing to summarise/send,
    # so we abort the whole run rather than emailing an empty newsletter.
    try:
        await fetcher.run_fetch(today)
    except Exception as e:
        print(f"[scheduler] fetch error: {e}")
        return

    # The fetcher creates a "newsletter" row keyed by date. If for some
    # reason it didn't (e.g., zero matching articles), there's nothing
    # downstream can do.
    newsletter = db.newsletter_get(today)
    if not newsletter:
        return

    # Stage 2: summarise. Failures here are non-fatal — admins can hit
    # "regenerate" later or send the newsletter with raw titles only.
    try:
        n = summarizer.summarize_newsletter(newsletter["id"])
        print(f"[scheduler] summarized {n} articles")
    except Exception as e:
        print(f"[scheduler] summarize error: {e}")

    # Stage 3: optional send. Only fires if the operator opted in via the
    # ``auto_send`` setting; otherwise the daily run stops at "draft ready".
    if db.cfg_get("auto_send") == "1":
        ok, msg = mailer.send_newsletter(today)
        print(f"[scheduler] auto-send: {msg}")


async def weekly_area_job():
    """Saturday pipeline: build each area's weekly issue (refresh weather, ensure
    a trail pick) and, if auto_send is on, email it to that area's subscribers.

    The issue's window is the coming week — Saturday (today) through the
    following Friday — so the 7-day forecast covers the week ahead. Building is
    blocking (httpx to NWS), so it runs in a worker thread to keep the event
    loop free."""
    today = dt_date.today()
    week_start = today.isoformat()
    week_end = (today + timedelta(days=6)).isoformat()
    print(f"[scheduler] weekly area job for week {week_start}…{week_end}")
    auto_send = db.cfg_get("auto_send") == "1"
    for area in areas.AREAS:
        slug = area["slug"]
        # Refresh the trail pool from komoot first (best-effort) so each week's
        # random pick can draw from freshly discovered hikes.
        try:
            added = await asyncio.to_thread(areas.refresh_area_trails, slug)
            print(f"[scheduler] komoot import {slug}: +{added} trails")
        except Exception as e:
            print(f"[scheduler] komoot import error for {slug}: {e}")
        try:
            await asyncio.to_thread(areas.build_area_issue, slug, week_start, week_end)
        except Exception as e:
            print(f"[scheduler] build error for {slug}: {e}")
            continue
        if auto_send:
            try:
                ok, msg = await asyncio.to_thread(
                    mailer.send_newsletter, week_start, slug
                )
                print(f"[scheduler] auto-send {slug}: {msg}")
            except Exception as e:
                print(f"[scheduler] send error for {slug}: {e}")


def _parse_time(t: str) -> tuple[int, int]:
    """Parse a 'HH:MM' string. Falls back to 07:00 on any malformed input
    so a typo in the settings page can't crash the scheduler."""
    try:
        h, m = t.split(":")
        return int(h), int(m)
    except Exception:
        return 7, 0


def start_scheduler() -> AsyncIOScheduler:
    """Create the singleton scheduler and register the daily cron job.
    Called once from the FastAPI app lifespan on startup."""
    global _scheduler
    # Pull the operator's configured time out of the DB. If the setting
    # is missing (fresh install) we default to 07:00.
    hour, minute = _parse_time(db.cfg_get("fetch_time") or "07:00")
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        daily_job,
        CronTrigger(hour=hour, minute=minute),
        # Stable job id so reschedule() can find this exact job, and
        # ``replace_existing`` makes start_scheduler() idempotent on
        # accidental double-start.
        id="daily_fetch",
        replace_existing=True,
    )
    # Weekly per-area issue build + send, fired every Saturday at weekly_send_time.
    whour, wminute = _parse_time(db.cfg_get("weekly_send_time") or "08:00")
    _scheduler.add_job(
        weekly_area_job,
        CronTrigger(day_of_week="sat", hour=whour, minute=wminute),
        id="weekly_areas",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] daily fetch at {hour:02d}:{minute:02d}; "
          f"weekly areas Sat {whour:02d}:{wminute:02d}")
    return _scheduler


def reschedule(fetch_time: str):
    """Update the daily run time without restarting the process. Called
    from the settings route when the operator changes ``fetch_time``."""
    if _scheduler:
        hour, minute = _parse_time(fetch_time)
        _scheduler.reschedule_job("daily_fetch", trigger=CronTrigger(hour=hour, minute=minute))


def stop_scheduler():
    """Shut the scheduler down on app exit. ``wait=False`` returns quickly
    rather than blocking on any in-flight job — we'd rather drop the run
    than hang shutdown."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
