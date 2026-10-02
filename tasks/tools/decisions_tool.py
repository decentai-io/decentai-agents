from decentai_sdk.base import ToolBase

from .agreement_rows import agreement_row
from .renewal_dates import iso, today


class DecisionsTool(ToolBase):
    id = "decisions"

    async def record(self, call):
        inputs = call.inputs
        ref = str(inputs["agreement_ref"])
        agreement = agreement_row(await call.resources.read_data("agreement", ref))
        if agreement["status"] != "active":
            return {"error": f"The agreement is {agreement['status']}; there is nothing left to decide."}, "error"
        decided_on = iso(inputs["decided_on"]) if inputs.get("decided_on") else today()
        if decided_on is None:
            return {"error": "decided_on must be YYYY-MM-DD."}, "error"
        decision = str(inputs["decision"])
        by = str(inputs["by"]).strip()
        record = await call.resources.create_data("decision", {
            "agreement_ref": ref, "decision": decision, "by": by, "decided_on": decided_on.isoformat(),
            "note": str(inputs.get("note") or "")})
        await call.resources.update_data("agreement", ref, {"decision": decision, "decision_by": by})
        return {"decision_ref": record["resource_ref"], "agreement_ref": ref, "decision": decision,
                "status": "active"}, "success"
