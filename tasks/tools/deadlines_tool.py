from datetime import timedelta

from decentai_sdk.base import ToolBase

from .agreement_rows import agreement_row, deadline_row, keys_of
from .renewal_dates import business_days_between, iso, today


class DeadlinesTool(ToolBase):
    id = "deadlines"

    async def upcoming(self, call):
        as_of_text = str(call.inputs.get("as_of") or "")
        as_of = iso(as_of_text) if as_of_text else today()
        if as_of is None:
            return {"error": "as_of must be YYYY-MM-DD."}, "error"
        within = int(call.inputs.get("within_days") if call.inputs.get("within_days") is not None else 30)
        owner = str(call.inputs.get("owner") or "").strip().lower()
        horizon = as_of + timedelta(days=within)
        agreements = {r["resource_ref"]: agreement_row(r) for r in await call.resources.list_data("agreement", {})}
        active = {ref: a for ref, a in agreements.items() if a["status"] == "active"
                  and (not owner or a["owner"].lower() == owner)}
        due = []
        for record in await call.resources.list_data("deadline", {}):
            d = deadline_row(record)
            agreement = active.get(d["agreement_ref"])
            if d["status"] != "open" or agreement is None:
                continue
            when = iso(d["date"])
            if when is None or when > horizon:
                continue
            due.append({"deadline_ref": d["deadline_ref"], "agreement_ref": d["agreement_ref"],
                        "agreement": agreement["name"], "kind": d["kind"], "title": d["title"],
                        "date": d["date"], "basis": d["basis"], "owner": d["owner"] or agreement["owner"],
                        "days_left": (when - as_of).days,
                        "business_days_left": business_days_between(as_of, when),
                        "passed": when < as_of, "decision": agreement["decision"]})
        due.sort(key=lambda d: (d["date"], d["agreement"]))
        awaiting, incomplete = [], []
        for ref, a in sorted(active.items(), key=lambda item: item[1]["name"]):
            deadline = iso(a["notice_deadline"])
            if deadline is None:
                if a["renewal_date"] and a["auto_renews"] == "no":
                    continue                       # nothing to give notice on
                reason = ("no renewal date" if not a["renewal_date"] else
                          "no notice period" if a["notice_days"] is None else
                          f"{a['notice_days']} days' notice, calendar or business unknown")
                incomplete.append({"agreement_ref": ref, "agreement": a["name"], "reason": reason})
                continue
            if a["decision"] == "undecided" and deadline <= horizon:
                awaiting.append({"agreement_ref": ref, "agreement": a["name"], "owner": a["owner"],
                                 "notice_deadline": a["notice_deadline"], "renewal_date": a["renewal_date"],
                                 "days_left": (deadline - as_of).days})
        return {"as_of": as_of.isoformat(), "due": due, "awaiting_decision": awaiting,
                "incomplete": incomplete}, "success"

    async def add(self, call):
        inputs = call.inputs
        ref = str(inputs["agreement_ref"])
        agreement = agreement_row(await call.resources.read_data("agreement", ref))
        when = iso(inputs["date"])
        if when is None:
            return {"error": "date must be YYYY-MM-DD."}, "error"
        fields = {"agreement_ref": ref, "kind": str(inputs["kind"]), "title": str(inputs["title"]).strip(),
                  "date": when.isoformat(), "basis": str(inputs.get("basis") or "calendar"),
                  "owner": str(inputs.get("owner") or agreement["owner"]), "status": "open"}
        record = await call.resources.create_data("deadline", fields)
        return {"deadline_ref": record["resource_ref"], "kind": fields["kind"], "title": fields["title"],
                "date": fields["date"]}, "success"

    async def complete(self, call):
        ref = str(call.inputs["deadline_ref"])
        current = deadline_row(await call.resources.read_data("deadline", ref))
        if current["status"] != "open":
            return {"error": f"The deadline is {current['status']}, not open."}, "error"
        done_on = iso(call.inputs["done_on"]) if call.inputs.get("done_on") else today()
        if done_on is None:
            return {"error": "done_on must be YYYY-MM-DD."}, "error"
        await call.resources.update_data("deadline", ref, {
            "status": "done", "done_on": done_on.isoformat(), "evidence": str(call.inputs["evidence"]).strip()})
        note = ("Notice recorded as given; the agreement stays active until its cancellation is "
                "confirmed with agreements.set_status." if current["kind"] == "notice" else
                "Recorded as done; the agreement's status is unchanged.")
        return {"deadline_ref": ref, "status": "done", "done_on": done_on.isoformat(), "note": note}, "success"
