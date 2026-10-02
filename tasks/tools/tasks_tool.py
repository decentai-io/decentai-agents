from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from decentai_sdk.base import ToolBase

from .dates import parse_iso


def _due(text: str):
    """A due date only when it is a real ISO date; anything else is an
    error to the caller, never a guess."""
    text = str(text or "").strip()
    if not text:
        return "", None
    found = parse_iso(text)
    if found is None or found.isoformat() != text:
        return "", f"due must be a date as YYYY-MM-DD, not '{text}'."
    return text, None


def _row(record):
    keys = record.get("keys") or {}
    return {
        "task_ref": str(record.get("resource_ref") or ""),
        "title": str(keys.get("title") or ""),
        "owner": str(keys.get("owner") or ""),
        "due": str(keys.get("due") or ""),
        "status": str(keys.get("status") or "open"),
        "blocker": str(keys.get("blocker") or ""),
        "source": str(keys.get("source") or ""),
        "evidence": str(keys.get("evidence") or ""),
    }


class TasksTool(ToolBase):
    id = "tasks"

    async def create(self, call):
        due, problem = _due(call.inputs.get("due"))
        if problem:
            return {"error": problem}, "error"
        owner = str(call.inputs.get("owner") or "").strip()
        record = await call.resources.create_data("task", {
            "title": str(call.inputs["title"]).strip(), "owner": owner,
            "due": due, "status": "open",
            "source": str(call.inputs.get("source") or ""),
            "note": str(call.inputs.get("note") or ""),
        })
        return {"task_ref": record["resource_ref"],
                "title": str(call.inputs["title"]).strip(), "owner": owner,
                "due": due, "status": "open", "unassigned": not owner}, "success"

    async def update(self, call):
        ref = str(call.inputs["task_ref"])
        current = _row(await call.resources.read_data("task", ref))
        changes = {}
        status = str(call.inputs.get("status") or "")
        if "due" in call.inputs:
            due, problem = _due(call.inputs.get("due"))
            if problem:
                return {"error": problem}, "error"
            changes["due"] = due
        if "owner" in call.inputs:
            changes["owner"] = str(call.inputs.get("owner") or "").strip()
        if call.inputs.get("note"):
            changes["note"] = str(call.inputs["note"])
        if call.inputs.get("evidence"):
            changes["evidence"] = str(call.inputs["evidence"])
        if status:
            if status == "done" and not (call.inputs.get("evidence") or current["evidence"]):
                return {"error": "done needs evidence: what shows it was done "
                                 "(a storage_ref, a file_ref, a message id, "
                                 "or a sentence from the person)."}, "error"
            if status == "blocked" and not (call.inputs.get("blocker") or current["blocker"]):
                return {"error": "blocked needs a blocker: say what stops it."}, "error"
            changes["status"] = status
            if status == "done":
                done_on = str(call.inputs.get("done_on") or "")
                if done_on and parse_iso(done_on) is None:
                    return {"error": "done_on must be YYYY-MM-DD."}, "error"
                changes["done_on"] = done_on or date.today().isoformat()
            if status != "blocked":
                changes["blocker"] = ""
        if call.inputs.get("blocker"):
            changes["blocker"] = str(call.inputs["blocker"])
        if not changes:
            return {"error": "Nothing to change."}, "error"
        await call.resources.update_data("task", ref, changes)
        after = _row(await call.resources.read_data("task", ref))
        return {k: after[k] for k in ("task_ref", "title", "owner", "due",
                                      "status", "blocker", "evidence")}, "success"

    async def list(self, call):
        filters = {}
        for name in ("owner", "status", "source"):
            if call.inputs.get(name):
                filters[name] = str(call.inputs[name])
        rows = [_row(r) for r in await call.resources.list_data("task", filters)]
        rows.sort(key=lambda r: (r["due"] or "9999-99-99", r["title"]))
        return {"tasks": [{k: r[k] for k in ("task_ref", "title", "owner", "due",
                                               "status", "blocker", "source")}
                          for r in rows], "total": len(rows)}, "success"

    async def overdue(self, call):
        as_of_text = str(call.inputs.get("as_of") or "")
        if as_of_text:
            as_of = parse_iso(as_of_text)
            if as_of is None:
                return {"error": "as_of must be YYYY-MM-DD."}, "error"
        else:
            zone_name = str(call.inputs.get("timezone") or "")
            try:
                zone = ZoneInfo(zone_name) if zone_name else timezone.utc
            except (ZoneInfoNotFoundError, ValueError):
                return {"error": f"'{zone_name}' is not an IANA time zone."}, "error"
            as_of = datetime.now(zone).date()
        within = int(call.inputs.get("within_days") or 5)
        filters = {"owner": str(call.inputs["owner"])} if call.inputs.get("owner") else {}
        rows = [_row(r) for r in await call.resources.list_data("task", filters)]

        overdue, due_soon, blocked, unassigned, undated = [], [], [], [], []
        for row in sorted(rows, key=lambda r: (r["due"] or "9999-99-99", r["title"])):
            if row["status"] == "blocked":
                blocked.append({k: row[k] for k in ("task_ref", "title", "owner", "blocker")})
                continue
            if row["status"] != "open":
                continue
            if not row["owner"]:
                unassigned.append({k: row[k] for k in ("task_ref", "title", "due")})
            due = parse_iso(row["due"]) if row["due"] else None
            if due is None:
                undated.append({k: row[k] for k in ("task_ref", "title", "owner")})
                continue
            delta = (due - as_of).days
            if delta < 0:
                overdue.append({**{k: row[k] for k in ("task_ref", "title", "owner", "due")},
                                "days_late": -delta})
            elif delta <= within:
                due_soon.append({**{k: row[k] for k in ("task_ref", "title", "owner", "due")},
                                 "days_left": delta})
        return {"as_of": as_of.isoformat(), "overdue": overdue, "due_soon": due_soon,
                "blocked": blocked, "unassigned": unassigned, "undated": undated}, "success"
