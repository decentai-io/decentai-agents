"""Projects, sections and labels: the names a task can be filed under."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .catalog import Catalog, project_row
from .todoist_api import TodoistError


class ProjectsTool(ToolBase):
    id = "projects"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            projects = client.projects()
        except TodoistError as exc:
            return failure(exc)
        rows = [project_row(p) for p in projects if not p.get("is_archived")]
        return {"projects": rows, "total": len(rows)}, "success"

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        body = {"name": str(call.inputs["name"]).strip()}
        try:
            if call.inputs.get("parent"):
                parent, problem = Catalog(client).project(call.inputs["parent"])
                if problem:
                    return problem, "error"
                body["parent_id"] = str(parent["id"])
            created = client.create_project(body)
        except TodoistError as exc:
            return failure(exc)
        if not created.get("id"):
            return {"error": "Todoist accepted the project but returned no id; "
                             "the outcome is unknown.", "kind": "unknown"}, "error"
        return project_row(created), "success"


class SectionsTool(ToolBase):
    id = "sections"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        catalog = Catalog(client)
        try:
            project_id = ""
            if call.inputs.get("project"):
                project, problem = catalog.project(call.inputs["project"])
                if problem:
                    return problem, "error"
                project_id = str(project["id"])
            sections = client.sections(project_id)
            names = catalog.project_names()
        except TodoistError as exc:
            return failure(exc)
        rows = [{"section_id": str(s.get("id") or ""), "name": str(s.get("name") or ""),
                 "project_id": str(s.get("project_id") or ""),
                 "project": names.get(str(s.get("project_id") or ""), "")}
                for s in sections if not s.get("is_archived")]
        return {"sections": rows, "total": len(rows)}, "success"


class LabelsTool(ToolBase):
    id = "labels"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            labels = client.labels()
        except TodoistError as exc:
            return failure(exc)
        rows = [{"label_id": str(l.get("id") or ""), "name": str(l.get("name") or "")}
                for l in labels]
        return {"labels": rows, "total": len(rows)}, "success"
