"""Dates, deterministically, and the one sum this agent does: a
deadline some days before a date, in calendar days or in business
days (Monday to Friday). A date is accepted only when it is exact:
ISO, or a written day, month and year. "December" is a month, not a
date, and "60 days" without calendar or business is not a basis."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_MONTH_ONLY = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I)
_DAYS = re.compile(r"\b(\d{1,3})\)?\s*(?:\(\w+\)\s*)?(business|working|calendar)?\s*days?\b", re.I)
_MONTHS_PERIOD = re.compile(r"\b(\d{1,2})\)?\s*(?:\(\w+\)\s*)?months?\b", re.I)


def today() -> date:
    return datetime.now(timezone.utc).date()


def iso(text) -> Optional[date]:
    """Exactly YYYY-MM-DD, or nothing."""
    text = str(text or "").strip()
    if len(text) != 10:
        return None
    try:
        found = date.fromisoformat(text)
    except ValueError:
        return None
    return found if found.isoformat() == text else None


def resolve(text) -> Tuple[Optional[date], str]:
    """(date, how): 'iso' or 'written' for an exact date; 'month' when
    only a month is named (no day); '' otherwise."""
    raw = str(text or "").strip()
    if not raw:
        return None, ""
    match = _ISO.search(raw)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))), "iso"
        except ValueError:
            return None, ""
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        match = pattern.search(raw)
        if not match:
            continue
        day, month = (match.group(1), match.group(2)) if order == "dm" else (match.group(2), match.group(1))
        number = MONTHS.get(month.lower())
        if number is None:
            continue
        try:
            return date(int(match.group(3)), number, int(day)), "written"
        except ValueError:
            return None, ""
    if _MONTH_ONLY.search(raw):
        return None, "month"
    return None, ""


def notice_period(text) -> Tuple[Optional[int], str]:
    """(days, basis) read from a notice clause: basis is 'calendar',
    'business' or 'unknown'; a period in months is unknown in days
    (months are not a number of days) and returns (None, 'months')."""
    raw = str(text or "")
    match = _DAYS.search(raw)
    if match:
        word = (match.group(2) or "").lower()
        basis = "business" if word in ("business", "working") else "calendar" if word == "calendar" else "unknown"
        return int(match.group(1)), basis
    if _MONTHS_PERIOD.search(raw):
        return None, "months"
    return None, ""


def business_days_back(end: date, days: int) -> date:
    """The date `days` business days before `end` (Monday to Friday)."""
    cursor, left = end, days
    while left > 0:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            left -= 1
    return cursor


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


def notice_deadline(renewal: Optional[date], days: Optional[int], basis: str) -> Tuple[Optional[date], str]:
    """(deadline, ask): the deadline when everything needed is known,
    else what to ask for."""
    if renewal is None:
        return None, "the exact renewal date (YYYY-MM-DD)"
    if days is None:
        return None, "the notice period in days"
    if basis == "calendar":
        return renewal - timedelta(days=days), ""
    if basis == "business":
        return business_days_back(renewal, days), ""
    return None, f"whether the {days} days' notice are calendar or business days"
