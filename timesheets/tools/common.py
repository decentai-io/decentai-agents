"""What the three tools share: weeks, hours, and records as rows.

Hours are kept as text with two decimals ("1.50") and reckoned as
Decimal, so a week adds up to exactly what its days add up to. A week
is an ISO week, "2026-W37", Monday to Sunday.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
CENT = Decimal("0.01")
DAY_LIMIT = Decimal(24)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def today() -> date:
    return datetime.now(timezone.utc).date()


def iso_date(text: Any) -> Optional[date]:
    text = str(text or "").strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def week_of(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_days(week: Any) -> Optional[List[date]]:
    found = WEEK.match(str(week or "").strip())
    if not found:
        return None
    try:
        monday = date.fromisocalendar(int(found.group(1)), int(found.group(2)), 1)
    except ValueError:
        return None
    return [monday + timedelta(days=i) for i in range(7)]


def hours_of(value: Any) -> Optional[Decimal]:
    try:
        hours = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not hours.is_finite():
        return None
    return hours.quantize(CENT, rounding=ROUND_HALF_UP)


def quarter_hours(minutes: float) -> Decimal:
    """A length of time to the nearest quarter hour, never less than one."""
    quarters = (Decimal(str(minutes)) / 15).to_integral_value(rounding=ROUND_HALF_UP)
    return (max(quarters, Decimal(1)) * Decimal("0.25")).quantize(CENT)


def as_text(hours: Decimal) -> str:
    return str(hours.quantize(CENT))


def code_of(value: Any) -> str:
    return str(value or "").strip().upper()


def keys_of(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (record or {}).get("keys") or {}


def entry_row(ref: str, keys: Dict[str, Any]) -> Dict[str, Any]:
    return {"entry_ref": ref, "date": str(keys.get("date") or ""),
            "week": str(keys.get("week") or ""),
            "project_code": str(keys.get("project_code") or ""),
            "hours": float(hours_of(keys.get("hours")) or 0),
            "note": str(keys.get("note") or ""),
            "source": str(keys.get("source") or "manual"),
            "event_id": str(keys.get("event_id") or ""),
            "status": str(keys.get("status") or "draft")}


def project_row(ref: str, keys: Dict[str, Any]) -> Dict[str, Any]:
    row = {"project_ref": ref, "code": str(keys.get("code") or ""),
           "name": str(keys.get("name") or ""), "client": str(keys.get("client") or ""),
           "billable": keys.get("billable") == "yes",
           "status": str(keys.get("status") or "active")}
    budget = hours_of(keys.get("budget_hours")) if keys.get("budget_hours") else None
    if budget is not None:
        row["budget_hours"] = float(budget)
    return row


async def projects(call) -> Dict[str, Dict[str, Any]]:
    """Every project by code: {"ref", "keys"}."""
    return {code_of(keys_of(r).get("code")): {"ref": str(r.get("resource_ref") or ""),
                                               "keys": keys_of(r)}
            for r in await call.resources.list_data("project", {})}


async def entries(call, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return [entry_row(str(r.get("resource_ref") or ""), keys_of(r))
            for r in await call.resources.list_data("entry", filters or {})]


async def week_record(call, week: str) -> Optional[Dict[str, Any]]:
    found = await call.resources.list_data("week", {"week": week})
    return found[0] if found else None


async def locked(call, week: str) -> bool:
    return keys_of(await week_record(call, week)).get("status") == "submitted"
