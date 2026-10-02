"""Rows out of records, and the deadlines an agreement implies."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .renewal_dates import iso, notice_deadline


def keys_of(record: Dict[str, Any]) -> Dict[str, Any]:
    return record.get("keys") or {}


def agreement_row(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = keys_of(record)
    row = {"agreement_ref": str(record.get("resource_ref") or "")}
    for name in ("name", "counterparty", "kind", "owner", "reference", "start_date", "renewal_date",
                 "notice_deadline", "currency", "decision_by", "evidence", "note"):
        row[name] = str(keys.get(name) or "")
    row["notice_basis"] = str(keys.get("notice_basis") or "unknown")
    row["auto_renews"] = str(keys.get("auto_renews") or "unknown")
    row["status"] = str(keys.get("status") or "active")
    row["decision"] = str(keys.get("decision") or "undecided")
    row["notice_days"] = None if keys.get("notice_days") in (None, "") else int(float(keys["notice_days"]))
    row["value"] = None if keys.get("value") in (None, "") else float(keys["value"])
    return row


def deadline_row(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = keys_of(record)
    return {"deadline_ref": str(record.get("resource_ref") or ""),
            "agreement_ref": str(keys.get("agreement_ref") or ""),
            "kind": str(keys.get("kind") or "custom"), "title": str(keys.get("title") or ""),
            "date": str(keys.get("date") or ""), "basis": str(keys.get("basis") or "calendar"),
            "owner": str(keys.get("owner") or ""), "status": str(keys.get("status") or "open"),
            "done_on": str(keys.get("done_on") or ""), "evidence": str(keys.get("evidence") or "")}


def derive(fields: Dict[str, Any]) -> List[str]:
    """Fills notice_deadline in an agreement's fields from renewal_date,
    notice_days and notice_basis; returns the asks when it cannot."""
    asks = []
    renewal = iso(fields.get("renewal_date"))
    if not fields.get("renewal_date"):
        asks.append("the exact renewal date (YYYY-MM-DD)")
    days = fields.get("notice_days")
    days = None if days in (None, "") else int(days)
    if days is None:
        asks.append("the notice period in days")
    deadline, ask = notice_deadline(renewal, days, str(fields.get("notice_basis") or "unknown"))
    if ask and ask not in asks:
        asks.append(ask)
    fields["notice_deadline"] = deadline.isoformat() if deadline else ""
    if fields.get("auto_renews", "unknown") == "unknown":
        asks.append("whether the agreement renews automatically")
    return asks


def implied_deadlines(agreement: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The renewal and notice deadlines an agreement's dates imply."""
    out = []
    if agreement.get("renewal_date"):
        out.append({"kind": "renewal", "title": f"{agreement['name']} renews",
                    "date": agreement["renewal_date"], "basis": "calendar"})
    if agreement.get("notice_deadline"):
        basis = agreement.get("notice_basis") or "calendar"
        out.append({"kind": "notice", "title": f"Last day to give notice on {agreement['name']}",
                    "date": agreement["notice_deadline"],
                    "basis": basis if basis in ("calendar", "business") else "calendar"})
    return out


async def sync_deadlines(call, agreement_ref: str, agreement: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Makes the open renewal/notice deadlines match the agreement:
    moves the ones that exist, creates the ones that do not, cancels
    the ones no longer implied. Returns the open renewal/notice rows."""
    existing = [deadline_row(r) for r in await call.resources.list_data("deadline", {"agreement_ref": agreement_ref})]
    open_by_kind = {d["kind"]: d for d in existing if d["status"] == "open" and d["kind"] in ("renewal", "notice")}
    wanted = {d["kind"]: d for d in implied_deadlines(agreement)}
    rows = []
    for kind, spec in wanted.items():
        fields = {"agreement_ref": agreement_ref, "kind": kind, "title": spec["title"], "date": spec["date"],
                  "basis": spec["basis"], "owner": agreement.get("owner") or "", "status": "open"}
        if kind in open_by_kind:
            ref = open_by_kind[kind]["deadline_ref"]
            await call.resources.update_data("deadline", ref, fields)
        else:
            ref = (await call.resources.create_data("deadline", fields))["resource_ref"]
        rows.append({"deadline_ref": ref, **spec})
    for kind, row in open_by_kind.items():
        if kind not in wanted:
            await call.resources.update_data("deadline", row["deadline_ref"], {"status": "cancelled"})
    return rows


async def close_deadlines(call, agreement_ref: str) -> int:
    closed = 0
    for record in await call.resources.list_data("deadline", {"agreement_ref": agreement_ref}):
        if keys_of(record).get("status") == "open":
            await call.resources.update_data("deadline", record["resource_ref"], {"status": "cancelled"})
            closed += 1
    return closed
