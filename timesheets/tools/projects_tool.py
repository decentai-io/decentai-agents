"""The projects time is logged against, and the rules that place a
meeting on one."""

from decimal import Decimal

from decentai_sdk.base import ToolBase

from .common import (CODE, code_of, entries, hours_of, keys_of, project_row,
                     projects)

FIELDS = ("title", "attendee", "location")


class ProjectsTool(ToolBase):
    id = "projects"

    async def add(self, call):
        inputs = call.inputs
        code = code_of(inputs["code"])
        if not CODE.match(code):
            return {"error": "A project code is letters, digits, - or _, up to 20, "
                             "like SHOW or RIVERSIDE-MOVE."}, "error"
        if code in await projects(call):
            return {"error": f"There is already a project {code}."}, "error"
        fields = {"code": code, "name": str(inputs["name"]).strip(),
                  "billable": "yes" if inputs.get("billable", True) else "no",
                  "status": "active"}
        if inputs.get("client"):
            fields["client"] = str(inputs["client"]).strip()
        if inputs.get("budget_hours") is not None:
            budget = hours_of(inputs["budget_hours"])
            if budget is None or budget <= 0:
                return {"error": "budget_hours must be a positive number."}, "error"
            fields["budget_hours"] = str(budget)
        record = await call.resources.create_data("project", fields)
        return {"project": project_row(record["resource_ref"], fields)}, "success"

    async def list(self, call):
        logged = {}
        for entry in await entries(call):
            logged[entry["project_code"]] = logged.get(entry["project_code"], Decimal(0)) \
                + (hours_of(entry["hours"]) or Decimal(0))
        rows = []
        for code, project in sorted((await projects(call)).items()):
            row = project_row(project["ref"], project["keys"])
            if row["status"] != "active" and not call.inputs.get("include_archived"):
                continue
            row["logged_hours"] = float(logged.get(code, Decimal(0)))
            if "budget_hours" in row:
                row["budget_left"] = row["budget_hours"] - row["logged_hours"]
            rows.append(row)
        return {"projects": rows}, "success"

    async def update(self, call):
        inputs = call.inputs
        code = code_of(inputs["code"])
        project = (await projects(call)).get(code)
        if project is None:
            return {"error": f"No project {code}."}, "error"
        changes = {}
        for name in ("name", "client"):
            if inputs.get(name) is not None:
                changes[name] = str(inputs[name]).strip()
        if inputs.get("billable") is not None:
            changes["billable"] = "yes" if inputs["billable"] else "no"
        if inputs.get("status"):
            changes["status"] = str(inputs["status"])
        if inputs.get("budget_hours") is not None:
            budget = hours_of(inputs["budget_hours"])
            if budget is None or budget <= 0:
                return {"error": "budget_hours must be a positive number."}, "error"
            changes["budget_hours"] = str(budget)
        if not changes:
            return {"error": "Nothing to change."}, "error"
        await call.resources.update_data("project", project["ref"], changes)
        return {"project": project_row(project["ref"], {**project["keys"], **changes})}, "success"

    async def add_rule(self, call):
        inputs = call.inputs
        match = str(inputs["match"]).strip()
        field = str(inputs.get("field") or "title")
        code = code_of(inputs["project_code"])
        if field not in FIELDS:
            return {"error": "field is title, attendee or location."}, "error"
        project = (await projects(call)).get(code)
        if project is None or project["keys"].get("status") != "active":
            return {"error": f"No active project {code}."}, "error"
        for rule in await call.resources.list_data("rule", {}):
            keys = keys_of(rule)
            if keys.get("field") == field and str(keys.get("match") or "").lower() == match.lower():
                return {"error": f"A rule for {field} containing '{match}' already places "
                                 f"meetings on {keys.get('project_code')}."}, "error"
        fields = {"match": match, "field": field, "project_code": code}
        record = await call.resources.create_data("rule", fields)
        return {"rule_ref": record["resource_ref"], **fields}, "success"

    async def rules(self, call):
        return {"rules": [{"rule_ref": str(r.get("resource_ref") or ""),
                           "match": str(keys_of(r).get("match") or ""),
                           "field": str(keys_of(r).get("field") or "title"),
                           "project_code": str(keys_of(r).get("project_code") or "")}
                          for r in await call.resources.list_data("rule", {})]}, "success"

    async def remove_rule(self, call):
        ref = str(call.inputs["rule_ref"])
        removed = await call.resources.delete_data("rule", ref)
        if not removed:
            return {"error": f"No rule {ref}."}, "error"
        return {"removed": True, "rule_ref": ref}, "success"
