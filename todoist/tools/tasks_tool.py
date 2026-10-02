"""Tasks in the person's Todoist: read, filed, changed, ticked off."""

from datetime import date

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .catalog import API_PRIORITY, Catalog, task_row
from .todoist_api import TodoistError


class TasksTool(ToolBase):
    id = "tasks"

    # -- reads -----------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        query = str(inputs.get("query") or "").strip()
        narrowed = any(inputs.get(k) for k in ("project", "section", "label"))
        if query and narrowed:
            # Todoist's filter endpoint takes nothing but the query; say
            # it in the query (#Project, /Section, @label) instead.
            return {"error": "Give either a filter query or project/section/label, "
                             "not both — a query can say '#Office Move & @calls'.",
                    "kind": "invalid"}, "error"
        most = int(inputs.get("max_results") or 20)
        cursor = str(inputs.get("cursor") or "")
        catalog = Catalog(client)
        try:
            if query:
                page = client.filter_page(query, most, cursor)
            else:
                project_id = section_id = label = ""
                if inputs.get("project"):
                    project, problem = catalog.project(inputs["project"])
                    if problem:
                        return problem, "error"
                    project_id = str(project["id"])
                if inputs.get("section"):
                    section, problem = catalog.section(inputs["section"], project_id)
                    if problem:
                        return problem, "error"
                    section_id = str(section["id"])
                if inputs.get("label"):
                    labels, problem = catalog.label_names([inputs["label"]])
                    if problem:
                        return problem, "error"
                    label = labels[0]
                page = client.tasks_page(project_id=project_id, section_id=section_id,
                                         label=label, limit=most, cursor=cursor)
            names = catalog.project_names()
        except TodoistError as exc:
            return failure(exc)
        rows = [task_row(t, names) for t in (page.get("results") or [])[:most]]
        next_cursor = str(page.get("next_cursor") or "")
        return {"tasks": rows, "more": bool(next_cursor),
                "next_cursor": next_cursor}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        task_id = str(call.inputs["task_id"])
        try:
            task = client.task(task_id)
            comments = client.comment_count(task_id)
            names = Catalog(client).project_names()
        except TodoistError as exc:
            return failure(exc)
        return {**task_row(task, names),
                "description": str(task.get("description") or ""),
                "completed": bool(task.get("checked")),
                "parent_id": str(task.get("parent_id") or ""),
                "added_at": str(task.get("added_at") or ""),
                "comment_count": comments}, "success"

    # -- writes ----------------------------------------------------------
    @staticmethod
    def _fields(inputs, catalog):
        """The fields create and update share, checked before anything is
        sent: a due date that is a date, a priority the app knows, labels
        that exist."""
        body = {}
        for field in ("content", "description"):
            if inputs.get(field) is not None and str(inputs.get(field)) != "":
                body[field] = str(inputs[field])
        if inputs.get("due_string") and inputs.get("due_date"):
            return None, {"error": "Give due_string or due_date, not both.",
                          "kind": "invalid"}
        if inputs.get("due_string"):
            body["due_string"] = str(inputs["due_string"])
        if inputs.get("due_date"):
            try:
                body["due_date"] = date.fromisoformat(str(inputs["due_date"])).isoformat()
            except ValueError:
                return None, {"error": "due_date must be YYYY-MM-DD; use due_string "
                                       "for words like 'next Monday'.", "kind": "invalid"}
        if inputs.get("priority"):
            body["priority"] = API_PRIORITY[str(inputs["priority"]).upper()]
        if inputs.get("labels") is not None:
            labels, problem = catalog.label_names(list(inputs["labels"]))
            if problem:
                return None, problem
            body["labels"] = labels
        return body, None

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        catalog = Catalog(client)
        try:
            body, problem = self._fields(inputs, catalog)
            if problem:
                return problem, "error"
            project_id = ""
            if inputs.get("project"):
                project, problem = catalog.project(inputs["project"])
                if problem:
                    return problem, "error"
                project_id = body["project_id"] = str(project["id"])
            if inputs.get("section"):
                section, problem = catalog.section(inputs["section"], project_id)
                if problem:
                    return problem, "error"
                body["section_id"] = str(section["id"])
                body["project_id"] = str(section.get("project_id") or project_id)
            await call.progress(f"Adding '{body['content']}' to Todoist")
            created = client.create_task(body)
        except TodoistError as exc:
            return failure(exc)
        if not created.get("id"):
            return {"error": "Todoist accepted the task but returned no id; "
                             "the outcome is unknown.", "kind": "unknown"}, "error"
        try:
            names = catalog.project_names()
        except TodoistError:
            names = {}   # the task exists; a missing project name is not a failure
        return task_row(created, names), "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        catalog = Catalog(client)
        try:
            body, problem = self._fields(call.inputs, catalog)
            if problem:
                return problem, "error"
            if not body:
                return {"error": "Nothing to change: give content, description, "
                                 "due_string, due_date, priority or labels.",
                        "kind": "invalid"}, "error"
            updated = client.update_task(str(call.inputs["task_id"]), body)
        except TodoistError as exc:
            return failure(exc)
        try:
            names = catalog.project_names()
        except TodoistError:
            names = {}
        return task_row(updated, names), "success"

    async def complete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        task_id = str(call.inputs["task_id"])
        try:
            task = client.task(task_id)
            if task.get("checked"):
                return {"task_id": task_id, "content": str(task.get("content") or ""),
                        "completed": True, "already": True, "recurring": False}, "success"
            client.close_task(task_id)
        except TodoistError as exc:
            return failure(exc)
        return {"task_id": task_id, "content": str(task.get("content") or ""),
                "completed": True, "already": False,
                # Closing a recurring task moves it to its next date
                # rather than finishing it; the assistant should say so.
                "recurring": bool((task.get("due") or {}).get("is_recurring"))}, "success"

    async def reopen(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        task_id = str(call.inputs["task_id"])
        try:
            client.reopen_task(task_id)
        except TodoistError as exc:
            return failure(exc)
        return {"task_id": task_id, "reopened": True}, "success"

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        task_id = str(call.inputs["task_id"])
        try:
            # Read first: the answer names what was deleted, and an
            # unknown id is reported as one rather than as a deletion.
            task = client.task(task_id)
            client.delete_task(task_id)
        except TodoistError as exc:
            return failure(exc)
        return {"task_id": task_id, "content": str(task.get("content") or ""),
                "deleted": True}, "success"
