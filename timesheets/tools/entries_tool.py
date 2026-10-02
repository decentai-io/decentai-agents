"""Hours in: logged by hand, or turned from calendar meetings by the
user's own rules.

Nothing guesses a project. A meeting no rule places, or one that rules
place on two different projects, comes back to be assigned; a meeting
already on the timesheet is not logged twice; an all-day, cancelled or
declined one is not time spent.
"""

from datetime import datetime, time
from decimal import Decimal
from typing import Optional

from decentai_sdk.base import ToolBase

from .common import (DAY_LIMIT, as_text, code_of, entries, entry_row, hours_of,
                     iso_date, keys_of, locked, projects, quarter_hours, today,
                     week_days, week_of)


def _clock(text) -> Optional[time]:
    text = str(text or "").strip()
    if len(text) != 5:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


def _moment(text) -> Optional[datetime]:
    """A start or end with a time in it; a bare date is an all-day event."""
    text = str(text or "").strip()
    if len(text) <= 10:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _matches(rule, event) -> bool:
    needle = str(rule.get("match") or "").strip().lower()
    if not needle:
        return False
    field = rule.get("field") or "title"
    if field == "attendee":
        haystack = event.get("attendees") or ""
    elif field == "location":
        haystack = event.get("location") or ""
    else:
        haystack = event.get("summary") or event.get("title") or ""
    return needle in str(haystack).lower()


def _bad_hours(hours) -> bool:
    return hours is None or hours <= 0 or hours > DAY_LIMIT


class EntriesTool(ToolBase):
    id = "entries"

    @staticmethod
    async def _active(call, code):
        """(code, None) for an active project, or ("", a result to return)."""
        known = await projects(call)
        project = known.get(code)
        if project and project["keys"].get("status") == "active":
            return code, None
        active = sorted(c for c, p in known.items() if p["keys"].get("status") == "active")
        why = f"No active project {code}." if code else "Name a project."
        hint = f" Active projects: {', '.join(active)}." if active else " Add a project first."
        return "", ({"error": why + hint}, "error")

    @staticmethod
    async def _day_total(call, day, leave_out=""):
        return sum((hours_of(e["hours"]) or Decimal(0)
                    for e in await entries(call, {"date": day.isoformat()})
                    if e["entry_ref"] != leave_out), Decimal(0))

    async def log(self, call):
        inputs = call.inputs
        day = iso_date(inputs["date"]) if inputs.get("date") else today()
        if day is None:
            return {"error": "date must be YYYY-MM-DD."}, "error"
        code, problem = await self._active(call, code_of(inputs["project_code"]))
        if problem:
            return problem
        if inputs.get("hours") is not None:
            hours = hours_of(inputs["hours"])
        elif inputs.get("start") and inputs.get("end"):
            start, end = _clock(inputs["start"]), _clock(inputs["end"])
            if start is None or end is None:
                return {"error": "start and end are HH:MM."}, "error"
            minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
            if minutes <= 0:
                return {"error": "end is before start."}, "error"
            hours = quarter_hours(minutes)
        else:
            return {"error": "Give hours, or a start and an end time."}, "error"
        if _bad_hours(hours):
            return {"error": "Hours must be more than 0 and at most 24."}, "error"
        week = week_of(day)
        if await locked(call, week):
            return {"error": f"The week {week} is submitted; reopen it before "
                             f"changing it."}, "error"
        total = await self._day_total(call, day) + hours
        if total > DAY_LIMIT:
            return {"error": f"That would make {day.isoformat()} {as_text(total)} hours."}, "error"
        fields = {"date": day.isoformat(), "week": week, "project_code": code,
                  "hours": as_text(hours), "source": "manual", "status": "draft"}
        if inputs.get("note"):
            fields["note"] = str(inputs["note"]).strip()
        record = await call.resources.create_data("entry", fields)
        return {"entry": entry_row(record["resource_ref"], fields),
                "day_total": float(total)}, "success"

    async def from_events(self, call):
        inputs = call.inputs
        assign = {str(a.get("event_id") or ""): code_of(a.get("project_code"))
                  for a in inputs.get("assign") or []}
        known = await projects(call)
        active = {c for c, p in known.items() if p["keys"].get("status") == "active"}
        rules = [keys_of(r) for r in await call.resources.list_data("rule", {})]
        logged = {e["event_id"] for e in await entries(call) if e["event_id"]}
        weeks_locked = {}
        day_totals = {}
        created, unassigned, skipped = [], [], []

        for event in inputs["events"]:
            event_id = str(event.get("event_id") or "").strip()
            title = str(event.get("summary") or event.get("title") or "(no title)")
            if not event_id:
                skipped.append({"event_id": "", "title": title, "reason": "no event_id"})
                continue
            if event_id in logged:
                skipped.append({"event_id": event_id, "title": title,
                                "reason": "already on the timesheet"})
                continue
            if str(event.get("status") or "") == "cancelled" \
                    or str(event.get("response") or "") == "declined":
                skipped.append({"event_id": event_id, "title": title,
                                "reason": "cancelled or declined"})
                continue
            start, end = _moment(event.get("start")), _moment(event.get("end"))
            if event.get("all_day") or start is None or end is None:
                skipped.append({"event_id": event_id, "title": title,
                                "reason": "all-day, or no start and end times"})
                continue
            minutes = (end - start).total_seconds() / 60
            if minutes <= 0:
                skipped.append({"event_id": event_id, "title": title,
                                "reason": "ends before it starts"})
                continue
            hours = quarter_hours(minutes)
            day = start.date()          # the date where the meeting was, as written
            week = week_of(day)
            if week not in weeks_locked:
                weeks_locked[week] = await locked(call, week)
            if weeks_locked[week]:
                skipped.append({"event_id": event_id, "title": title,
                                "reason": f"the week {week} is submitted"})
                continue

            code, reason = assign.get(event_id, ""), ""
            if code and code not in active:
                code, reason = "", f"no active project {assign[event_id]}"
            elif not code:
                placed = []
                for rule in rules:
                    target = code_of(rule.get("project_code"))
                    if target in active and target not in placed and _matches(rule, event):
                        placed.append(target)
                if len(placed) == 1:
                    code = placed[0]
                elif placed:
                    reason = "rules place it on more than one project: " + ", ".join(placed)
                else:
                    reason = "no rule places it"
            if not code:
                unassigned.append({"event_id": event_id, "title": title,
                                   "date": day.isoformat(), "hours": float(hours),
                                   "reason": reason})
                continue

            if day not in day_totals:
                day_totals[day] = await self._day_total(call, day)
            if day_totals[day] + hours > DAY_LIMIT:
                skipped.append({"event_id": event_id, "title": title,
                                "reason": f"{day.isoformat()} would pass 24 hours"})
                continue
            fields = {"date": day.isoformat(), "week": week, "project_code": code,
                      "hours": as_text(hours), "note": title, "source": "calendar",
                      "event_id": event_id, "status": "draft"}
            record = await call.resources.create_data("entry", fields)
            day_totals[day] += hours
            logged.add(event_id)
            created.append({"entry_ref": record["resource_ref"], "event_id": event_id,
                            "title": title, "date": day.isoformat(),
                            "project_code": code, "hours": float(hours)})
        return {"created": created, "unassigned": unassigned, "skipped": skipped}, "success"

    async def list(self, call):
        inputs = call.inputs
        if inputs.get("from_date") or inputs.get("to_date"):
            lo, hi = iso_date(inputs.get("from_date")), iso_date(inputs.get("to_date"))
            if lo is None or hi is None:
                return {"error": "Give both from_date and to_date, as YYYY-MM-DD."}, "error"
            rows = [e for e in await entries(call) if lo.isoformat() <= e["date"] <= hi.isoformat()]
        else:
            week = str(inputs.get("week") or week_of(today())).strip()
            if week_days(week) is None:
                return {"error": "week is an ISO week, like 2026-W37."}, "error"
            rows = await entries(call, {"week": week})
        code = code_of(inputs.get("project_code"))
        if code:
            rows = [e for e in rows if e["project_code"] == code]
        rows.sort(key=lambda e: (e["date"], e["project_code"], e["entry_ref"]))
        total = sum((hours_of(e["hours"]) or Decimal(0) for e in rows), Decimal(0))
        return {"entries": rows, "total": float(total)}, "success"

    async def update(self, call):
        inputs = call.inputs
        ref = str(inputs["entry_ref"])
        try:
            current = keys_of(await call.resources.read_data("entry", ref))
        except Exception:
            return {"error": f"No entry {ref}."}, "error"
        if await locked(call, str(current.get("week") or "")):
            return {"error": f"The week {current.get('week')} is submitted; reopen it "
                             f"before changing it."}, "error"
        changes = {}
        day = iso_date(current.get("date"))
        if inputs.get("date"):
            day = iso_date(inputs["date"])
            if day is None:
                return {"error": "date must be YYYY-MM-DD."}, "error"
            changes["date"], changes["week"] = day.isoformat(), week_of(day)
            if await locked(call, changes["week"]):
                return {"error": f"The week {changes['week']} is submitted."}, "error"
        if inputs.get("project_code"):
            code, problem = await self._active(call, code_of(inputs["project_code"]))
            if problem:
                return problem
            changes["project_code"] = code
        hours = hours_of(current.get("hours")) or Decimal(0)
        if inputs.get("hours") is not None:
            hours = hours_of(inputs["hours"])
            if _bad_hours(hours):
                return {"error": "Hours must be more than 0 and at most 24."}, "error"
            changes["hours"] = as_text(hours)
        if inputs.get("note") is not None:
            changes["note"] = str(inputs["note"]).strip()
        if not changes:
            return {"error": "Nothing to change."}, "error"
        total = await self._day_total(call, day, leave_out=ref) + hours
        if total > DAY_LIMIT:
            return {"error": f"That would make {day.isoformat()} {as_text(total)} hours."}, "error"
        await call.resources.update_data("entry", ref, changes)
        return {"entry": entry_row(ref, {**current, **changes})}, "success"

    async def remove(self, call):
        ref = str(call.inputs["entry_ref"])
        try:
            current = keys_of(await call.resources.read_data("entry", ref))
        except Exception:
            return {"error": f"No entry {ref}."}, "error"
        if await locked(call, str(current.get("week") or "")):
            return {"error": f"The week {current.get('week')} is submitted; reopen it "
                             f"before changing it."}, "error"
        await call.resources.delete_data("entry", ref)
        return {"removed": True, "entry_ref": ref}, "success"
