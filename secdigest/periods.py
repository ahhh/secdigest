"""ISO-week and calendar-month helpers for digest grouping.

Weekly and monthly digests need a consistent way to bucket per-day articles
into a longer reporting window. These helpers normalise an arbitrary
``YYYY-MM-DD`` string into the bounds and label of the ISO week or
calendar month it falls in. ISO weeks (Mon–Sun) are used so the weekly
digest layout doesn't drift across year boundaries.
"""
from datetime import date, timedelta


# Internal: ``date.fromisoformat`` already parses YYYY-MM-DD; wrapping it
# gives us one place to swap the parser later (e.g., to be more lenient).
def _parse(date_str: str) -> date:
    return date.fromisoformat(date_str)


def iso_week_bounds(date_str: str) -> tuple[str, str]:
    """Return (monday, sunday) ISO date strings for the ISO week containing date_str."""
    d = _parse(date_str)
    # ``weekday()`` returns 0 for Monday … 6 for Sunday, so subtracting it
    # walks backwards to that week's Monday regardless of which day we
    # started on. Sunday is then exactly 6 days later.
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def iso_week_label(date_str: str) -> str:
    """e.g. '2026-W18' for any date in that week."""
    # ``isocalendar()`` is the safe way to get the ISO year+week pair —
    # the calendar year and ISO year can disagree near Jan 1 / Dec 31
    # (e.g., 2026-01-01 may belong to ISO week 53 of 2025).
    d = _parse(date_str)
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def month_bounds(date_str: str) -> tuple[str, str]:
    """Return (first, last) ISO date strings for the month containing date_str."""
    d = _parse(date_str)
    first = d.replace(day=1)
    # To find the last day of the month, jump to the first day of the
    # *next* month and step back one day. This avoids hard-coding month
    # lengths and handles February / leap years for free.
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last = next_first - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def month_label(date_str: str) -> str:
    """e.g. 'May 2026'."""
    # ``%B`` is the full month name ("May"), ``%Y`` the 4-digit year.
    return _parse(date_str).strftime("%B %Y")
