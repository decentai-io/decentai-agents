"""Dates, deterministically. A date is accepted only when it is
unambiguous: ISO, a written date with a month name, or a weekday name
relative to a known meeting date. "Next week", "soon", "12/09" are not
dates here — they stay as text for a person to resolve."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional, Tuple

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:\s+(\d{4}))?\b")
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b")
_WEEKDAY = re.compile(r"\b(?:(next|this)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def parse_iso(text: str) -> Optional[date]:
    match = _ISO.search(str(text or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def resolve(text: str, anchor: Optional[date]) -> Tuple[Optional[date], str]:
    """(date, how) — how is 'iso', 'written', 'weekday', or '' when the
    text is not an unambiguous date. A written date without a year
    takes the anchor's year (the next year if that would be in the
    past); a weekday needs an anchor and means the first such day
    strictly after it."""
    raw = str(text or "").strip()
    if not raw:
        return None, ""
    found = parse_iso(raw)
    if found:
        return found, "iso"
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        match = pattern.search(raw)
        if not match:
            continue
        day, month = (match.group(1), match.group(2)) if order == "dm" else (match.group(2), match.group(1))
        number = MONTHS.get(month.lower())
        if number is None:
            continue
        year = match.group(3)
        try:
            if year:
                return date(int(year), number, int(day)), "written"
            if anchor is None:
                return None, ""
            candidate = date(anchor.year, number, int(day))
            if candidate < anchor:
                candidate = date(anchor.year + 1, number, int(day))
            return candidate, "written"
        except ValueError:
            return None, ""
    match = _WEEKDAY.search(raw)
    if match and anchor is not None:
        target = WEEKDAYS.index(match.group(2).lower())
        ahead = (target - anchor.weekday()) % 7 or 7
        return anchor + timedelta(days=ahead), "weekday"
    if raw.lower() == "tomorrow" and anchor is not None:
        return anchor + timedelta(days=1), "weekday"
    return None, ""


def business_days_between(start: date, end: date) -> int:
    """Business days from start (exclusive) to end (inclusive); negative
    when end is before start."""
    if end < start:
        return -business_days_between(end, start)
    days, cursor = 0, start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days
