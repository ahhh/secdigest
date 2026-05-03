"""APScheduler daily job: fetch + summarize + optional auto-send."""
import asyncio
from datetime import date as dt_date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
import fetcher
import summarizer
import mailer

_scheduler: AsyncIOScheduler | None = None


async def daily_job():
    today = dt_date.today().isoformat()
    print(f"[scheduler] daily job starting for {today}")
    try:
        await fetcher.run_fetch(today)
    except Exception as e:
        print(f"[scheduler] fetch error: {e}")
        return

    newsletter = db.newsletter_get(today)
    if not newsletter:
        return

    try:
        count = summarizer.summarize_newsletter(newsletter["id"])
        print(f"[scheduler] summarized {count} articles")
    except Exception as e:
        print(f"[scheduler] summarize error: {e}")

    if db.cfg_get("auto_send") == "1":
        ok, msg = mailer.send_newsletter(today)
        print(f"[scheduler] auto-send: {msg}")


def _parse_time(fetch_time: str) -> tuple[int, int]:
    try:
        h, m = fetch_time.split(":")
        return int(h), int(m)
    except Exception:
        return 7, 0


def start_scheduler():
    global _scheduler
    fetch_time = db.cfg_get("fetch_time") or "07:00"
    hour, minute = _parse_time(fetch_time)

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        daily_job,
        CronTrigger(hour=hour, minute=minute),
        id="daily_fetch",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] started — daily job at {hour:02d}:{minute:02d}")
    return _scheduler


def reschedule(fetch_time: str):
    """Update the daily job trigger after settings change."""
    global _scheduler
    if not _scheduler:
        return
    hour, minute = _parse_time(fetch_time)
    _scheduler.reschedule_job(
        "daily_fetch",
        trigger=CronTrigger(hour=hour, minute=minute),
    )
    print(f"[scheduler] rescheduled to {hour:02d}:{minute:02d}")


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
