"""ISO-week and calendar-month helpers for digest grouping."""
from datetime import date, timedelta


def _parse(date_str: str) -> date:
    return date.fromisoformat(date_str)


def iso_week_bounds(date_str: str) -> tuple[str, str]:
    """Return (monday, sunday) ISO date strings for the ISO week containing date_str."""
    d = _parse(date_str)
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def iso_week_label(date_str: str) -> str:
    """e.g. '2026-W18' for any date in that week."""
    d = _parse(date_str)
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def month_bounds(date_str: str) -> tuple[str, str]:
    """Return (first, last) ISO date strings for the month containing date_str."""
    d = _parse(date_str)
    first = d.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last = next_first - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def month_label(date_str: str) -> str:
    """e.g. 'May 2026'."""
    return _parse(date_str).strftime("%B %Y")
