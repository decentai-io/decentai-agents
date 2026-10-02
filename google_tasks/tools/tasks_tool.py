"""Tasks: read a list, read one task whole, and change tasks.

Creating, editing, completing and reopening are level 1: a personal
task is private and inert, like a Gmail draft. Deleting is level 3 —
it cannot be taken back.
"""

from datetime import date

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .google_api import GoogleError, second

#: How much of a task's notes a row carries. Notes can be long; a row is
#: for choosing, and tasks.get reads the rest.
ROW_NOTE_CHARS = 300


def clip(text, most):
    text = str(text or "").strip()
    return text if len(text) <= most else text[:most].rstrip() + "…"


def task_row(task, list_id, note_chars=ROW_NOTE_CHARS):
    row = {
        "task_id": str(task.get("id") or ""),
        "list_id": str(list_id),
        "title": str(task.get("title") or ""),
        "status": str(task.get("status") or ""),
        # Google keeps only the day of a due date; the time is always
        # midnight UTC, so the date part is the date meant.
        "due": str(task.get("due") or "")[:10],
        "completed": second(task.get("completed")),
        "modified": second(task.get("updated")),
        "parent": str(task.get("parent") or ""),
        "link": str(task.get("webViewLink") or ""),
    }
    note = clip(task.get("notes"), note_chars)
    if note:
        row["note"] = note
    return row


def due_value(text):
    """(Google due or None to clear, problem). Google Tasks keeps a date,
    written as midnight UTC."""
    text = str(text or "").strip()
    if not text:
        return None, ""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None, f"due must be YYYY-MM-DD, not {text!r}."
    if len(text) != 10:
        return None, f"due must be YYYY-MM-DD, not {text!r}."
    return f"{day.isoformat()}T00:00:00.000Z", ""


class TasksTool(ToolBase):
    id = "tasks"

    @staticmethod
    def _list_id(client, wanted):
        """The list asked for, or the account's default list — resolved to
        its real id, so a row never carries an alias."""
        wanted = str(wanted or "").strip()
        if wanted:
            return wanted
        return str(client.get_list("@default").get("id") or "")

    # -- reads -----------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 20)
        page_token = str(call.inputs.get("page_token") or "")
        if page_token and not page_token.isdigit():
            return invalid("page_token is not one this agent returned.")
        offset = int(page_token or 0)
        status = str(call.inputs.get("status") or "open")
        due_before = str(call.inputs.get("due_before") or "").strip()
        if due_before:
            _, problem = due_value(due_before)
            if problem:
                return invalid(problem.replace("due", "due_before", 1))
        try:
            list_id = self._list_id(client, call.inputs.get("list_id"))
            if status == "open":
                tasks = client.all_tasks(list_id, showCompleted="false")
            else:
                # A task ticked off in Google's own apps is hidden as well
                # as completed; without showHidden it would not be seen.
                tasks = client.all_tasks(list_id, showCompleted="true", showHidden="true")
        except GoogleError as exc:
            return failure(exc)
        if status == "open":
            tasks = [t for t in tasks if t.get("status") != "completed"]
        elif status == "completed":
            tasks = [t for t in tasks if t.get("status") == "completed"]
        if due_before:
            tasks = [t for t in tasks if t.get("due") and str(t["due"])[:10] < due_before]
        # Google has no ordering to ask for: newest change first is sorted
        # here, and the page token is simply how far into that order.
        tasks.sort(key=lambda t: second(t.get("updated")), reverse=True)
        page = tasks[offset: offset + most]
        result = {"list_id": list_id, "tasks": [task_row(t, list_id) for t in page]}
        if offset + most < len(tasks):
            result["next_page_token"] = str(offset + most)
        return result, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        list_id, task_id = str(call.inputs["list_id"]), str(call.inputs["task_id"])
        most = int(call.inputs.get("max_note_chars") or 2000)
        try:
            task = client.get_task(list_id, task_id)
            children = [t for t in client.all_tasks(list_id, showCompleted="true",
                                                     showHidden="true")
                        if t.get("parent") == task_id]
        except GoogleError as exc:
            return failure(exc)
        row = task_row(task, list_id, note_chars=most)
        row["note"] = row.get("note", "")
        row["note_truncated"] = len(str(task.get("notes") or "").strip()) > most
        # Subtasks in the person's own order, as Google keeps it.
        children.sort(key=lambda t: str(t.get("position") or ""))
        row["subtasks"] = [{"task_id": str(t.get("id") or ""),
                            "title": str(t.get("title") or ""),
                            "status": str(t.get("status") or "")}
                           for t in children[:25]]
        return row, "success"

    # -- writes ----------------------------------------------------------
    @staticmethod
    def _fields(inputs):
        """The Google fields a create or update sets, or a problem. Only
        what was given is sent; an empty due clears it."""
        body = {}
        if "title" in inputs:
            body["title"] = str(inputs["title"]).strip()
        if "due" in inputs:
            due, problem = due_value(inputs["due"])
            if problem:
                return {}, problem
            body["due"] = due
        if "note" in inputs:
            body["notes"] = str(inputs["note"] or "")
        return body, ""

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        body, problem = self._fields(call.inputs)
        if problem:
            return invalid(problem)
        if body.get("due", 1) is None:
            body.pop("due")
        try:
            list_id = self._list_id(client, call.inputs.get("list_id"))
            created = client.create_task(list_id, body, str(call.inputs.get("parent") or ""))
        except GoogleError as exc:
            return failure(exc)
        return task_row(created, list_id), "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        body, problem = self._fields({k: v for k, v in call.inputs.items()
                                      if k not in ("list_id", "task_id")})
        if problem:
            return invalid(problem)
        if not body:
            return invalid("Nothing to change: give a title, due or note.")
        return self._patch(client, call, body)

    async def complete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        # Google stamps "completed" itself when the status turns.
        return self._patch(client, call, {"status": "completed"})

    async def reopen(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        # The completion time is cleared with the status, or the task would
        # read as open and completed at once.
        return self._patch(client, call, {"status": "needsAction", "completed": None})

    @staticmethod
    def _patch(client, call, body):
        list_id, task_id = str(call.inputs["list_id"]), str(call.inputs["task_id"])
        try:
            updated = client.update_task(list_id, task_id, body)
            if not updated:
                updated = client.get_task(list_id, task_id)
        except GoogleError as exc:
            return failure(exc)
        return task_row(updated, list_id), "success"

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        list_id, task_id = str(call.inputs["list_id"]), str(call.inputs["task_id"])
        try:
            task = client.get_task(list_id, task_id)
            client.delete_task(list_id, task_id)
        except GoogleError as exc:
            return failure(exc)
        return {"deleted": True, "task_id": task_id,
                "title": str(task.get("title") or "")}, "success"
