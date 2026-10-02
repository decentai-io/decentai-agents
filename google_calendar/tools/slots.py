"""Finding a time is arithmetic.

Given the busy periods of every calendar that could be checked, a
window of business days, working hours, avoid-windows (lunch) and a
buffer to keep clear around existing meetings, the candidates are the
slots of the wanted length that fit entirely in what is left. Nothing
here asks a model; the same inputs always give the same slots.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

STEP = timedelta(minutes=15)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")


def parse_when(text: str, zone: ZoneInfo) -> datetime:
    """ISO 8601, with or without an offset; a naive time is in ``zone``.
    A trailing Z is accepted."""
    text = str(text or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment


def parse_clock(text: str) -> time:
    hour, minute = str(text).split(":")
    return time(int(hour), int(minute))


def _subtract(free: List[Tuple[datetime, datetime]],
              busy: Tuple[datetime, datetime]) -> List[Tuple[datetime, datetime]]:
    start, end = busy
    kept = []
    for a, b in free:
        if end <= a or start >= b:
            kept.append((a, b))
            continue
        if start > a:
            kept.append((a, start))
        if end < b:
            kept.append((end, b))
    return kept


def find_slots(
    *, duration: timedelta, from_date: date, to_date: date, zone: ZoneInfo,
    working_start: time, working_end: time,
    avoid: List[Tuple[time, time]], buffer: timedelta,
    busy: List[Tuple[datetime, datetime]], not_before: Optional[datetime],
    limit: int,
) -> List[Dict[str, Any]]:
    """Slots on business days, inside working hours, outside avoid
    windows, clear of every busy period plus the buffer on both sides,
    at 15-minute steps; at most ``limit``, spread at most two per day
    so the user sees a choice of days before a choice of hours."""
    padded = [(s - buffer, e + buffer) for s, e in busy]
    found: List[Dict[str, Any]] = []
    day = from_date
    while day <= to_date and len(found) < limit:
        if day.weekday() < 5:
            free = [(datetime.combine(day, working_start, zone),
                     datetime.combine(day, working_end, zone))]
            for a_start, a_end in avoid:
                free = _subtract(free, (datetime.combine(day, a_start, zone),
                                        datetime.combine(day, a_end, zone)))
            for period in padded:
                free = _subtract(free, period)
            if not_before is not None:
                free = _subtract(free, (datetime.min.replace(tzinfo=timezone.utc),
                                        not_before))
            today = 0
            for a, b in free:
                cursor = _ceil(a)
                while cursor + duration <= b and today < 2 and len(found) < limit:
                    found.append({
                        "start": cursor.isoformat(),
                        "end": (cursor + duration).isoformat(),
                        "weekday": WEEKDAYS[day.weekday()],
                    })
                    today += 1
                    cursor += duration + buffer
                    cursor = _ceil(cursor)
                if today >= 2:
                    break
        day += timedelta(days=1)
    return found


def _ceil(moment: datetime) -> datetime:
    """Up to the next quarter hour, so slots read like meeting times."""
    minutes = (moment.minute // 15) * 15
    floored = moment.replace(minute=minutes, second=0, microsecond=0)
    return floored if floored == moment else floored + STEP
