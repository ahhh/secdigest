"""APScheduler daily job: fetch → summarize → optional auto-send."""
import asyncio
from datetime import date as dt_date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from secdigest import db
from secdigest import fetcher, summarizer, mailer

_scheduler: AsyncIOScheduler | None = None


async def daily_job():
    today = dt_date.today().isoformat()
    print(f"[scheduler] daily job for {today}")
    try:
        await fetcher.run_fetch(today)
    except Exception as e:
        print(f"[scheduler] fetch error: {e}")
        return

    newsletter = db.newsletter_get(today)
    if not newsletter:
        return

    try:
        n = summarizer.summarize_newsletter(newsletter["id"])
        print(f"[scheduler] summarized {n} articles")
    except Exception as e:
        print(f"[scheduler] summarize error: {e}")

    if db.cfg_get("auto_send") == "1":
        ok, msg = mailer.send_newsletter(today)
        print(f"[scheduler] auto-send: {msg}")


def _parse_time(t: str) -> tuple[int, int]:
    try:
        h, m = t.split(":")
        return int(h), int(m)
    except Exception:
        return 7, 0


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    hour, minute = _parse_time(db.cfg_get("fetch_time") or "07:00")
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        daily_job,
        CronTrigger(hour=hour, minute=minute),
        id="daily_fetch",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] daily fetch at {hour:02d}:{minute:02d}")
    return _scheduler


def reschedule(fetch_time: str):
    if _scheduler:
        hour, minute = _parse_time(fetch_time)
        _scheduler.reschedule_job("daily_fetch", trigger=CronTrigger(hour=hour, minute=minute))


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
