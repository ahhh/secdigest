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
from datetime import date as dt_date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from secdigest import db
from secdigest import fetcher, summarizer, mailer

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
    _scheduler.start()
    print(f"[scheduler] daily fetch at {hour:02d}:{minute:02d}")
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
