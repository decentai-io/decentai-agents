"""Tasks: read a list, read one task whole, and change tasks.

Creating, editing, completing and reopening are level 1: a personal
task is private and inert, like a mail draft. Deleting is level 3 —
it cannot be taken back.
"""

from datetime import date

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .graph_todo import GraphError, graph_date, graph_moment, second

#: How much of a task's note a row carries. A note can be pages long; a
#: row is for choosing, and tasks.get reads the rest.
ROW_NOTE_CHARS = 300


def clip(text, most):
    text = str(text or "").strip()
    return text if len(text) <= most else text[:most].rstrip() + "…"


def reminder_text(value):
    """A reminder the agent wrote is UTC; one set in the To Do app may be
    in another zone, and is shown with it rather than mis-converted."""
    zone = str((value or {}).get("timeZone") or "UTC")
    if zone.upper() in ("UTC", "ETC/UTC"):
        return graph_moment(value)
    return f"{str((value or {}).get('dateTime') or '')[:19]} ({zone})"


def task_row(task, list_id, note_chars=ROW_NOTE_CHARS):
    row = {
        "task_id": str(task.get("id") or ""),
        "list_id": str(list_id),
        "title": str(task.get("title") or ""),
        "status": str(task.get("status") or ""),
        "importance": str(task.get("importance") or "normal"),
        "due": graph_date(task.get("dueDateTime")),
        "completed": graph_date(task.get("completedDateTime")),
        "modified": second(task.get("lastModifiedDateTime")),
        "reminder": reminder_text(task.get("reminderDateTime")) if task.get("isReminderOn") else "",
    }
    note = clip((task.get("body") or {}).get("content"), note_chars)
    if note:
        row["note"] = note
    return row


def due_value(text):
    """(Graph dueDateTime or None to clear, problem). A due date is a day:
    written as midnight UTC, it reads back as the same day."""
    text = str(text or "").strip()
    if not text:
        return None, ""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None, f"due must be YYYY-MM-DD, not {text!r}."
    if len(text) != 10:
        return None, f"due must be YYYY-MM-DD, not {text!r}."
    return {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": "UTC"}, ""


def reminder_value(text):
    """(Graph reminderDateTime, problem). A reminder is a moment, and a
    moment without an offset is a guess about the person's zone."""
    text = str(text or "").strip()
    moment = second(text)
    if not moment or not (text.endswith("Z") or "+" in text[10:] or "-" in text[10:]):
        return None, (f"reminder must be an ISO 8601 date-time with an offset "
                      f"(2026-09-20T09:00:00+04:00) or Z, not {text!r}.")
    return {"dateTime": moment[:-1], "timeZone": "UTC"}, ""


class TasksTool(ToolBase):
    id = "tasks"

    @staticmethod
    def _list_id(client, wanted):
        """The list asked for, or the account's default list."""
        wanted = str(wanted or "").strip()
        if wanted:
            return wanted
        default = client.default_list()
        if default is None:
            raise GraphError("not_found", "This account has no default To Do "
                                          "list; name one with list_id.")
        return str(default.get("id") or "")

    # -- reads -----------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 20)
        page_token = str(call.inputs.get("page_token") or "")
        if page_token and not client.follows(page_token):
            return invalid("page_token is not one this agent returned.")
        filters = []
        status = str(call.inputs.get("status") or "open")
        if status == "open":
            filters.append("status ne 'completed'")
        elif status == "completed":
            filters.append("status eq 'completed'")
        due_before = str(call.inputs.get("due_before") or "").strip()
        if due_before:
            due, problem = due_value(due_before)
            if problem:
                return invalid(problem.replace("due", "due_before", 1))
            filters.append(f"dueDateTime/dateTime lt '{due['dateTime']}'")
        try:
            list_id = self._list_id(client, call.inputs.get("list_id"))
            page = client.tasks(list_id, filters=filters,
                                order="lastModifiedDateTime desc", top=most,
                                page_link=page_token)
        except GraphError as exc:
            return failure(exc)
        result = {"list_id": list_id,
                  "tasks": [task_row(t, list_id) for t in (page.get("value") or [])[:most]]}
        if page.get("@odata.nextLink"):
            result["next_page_token"] = str(page["@odata.nextLink"])
        return result, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        list_id, task_id = str(call.inputs["list_id"]), str(call.inputs["task_id"])
        most = int(call.inputs.get("max_note_chars") or 2000)
        try:
            task = client.get_task(list_id, task_id)
            items = client.checklist(list_id, task_id)
        except GraphError as exc:
            return failure(exc)
        row = task_row(task, list_id, note_chars=most)
        row["note"] = row.get("note", "")
        row["note_truncated"] = len(str((task.get("body") or {}).get("content") or "").strip()) > most
        row["created"] = second(task.get("createdDateTime"))
        row["checklist"] = [{"item_id": str(i.get("id") or ""),
                             "title": str(i.get("displayName") or ""),
                             "checked": bool(i.get("isChecked"))}
                            for i in items[:25]]
        return row, "success"

    # -- writes ----------------------------------------------------------
    def _fields(self, inputs):
        """The Graph fields a create or update sets, or a problem. Only
        what was given is sent; an empty due or reminder clears it."""
        body, problem = {}, ""
        if "title" in inputs:
            body["title"] = str(inputs["title"]).strip()
        if "due" in inputs:
            due, problem = due_value(inputs["due"])
            if problem:
                return {}, problem
            body["dueDateTime"] = due
        if inputs.get("importance"):
            body["importance"] = str(inputs["importance"])
        if "note" in inputs:
            body["body"] = {"content": str(inputs["note"] or ""), "contentType": "text"}
        if "reminder" in inputs:
            if str(inputs["reminder"] or "").strip():
                reminder, problem = reminder_value(inputs["reminder"])
                if problem:
                    return {}, problem
                body["reminderDateTime"] = reminder
                body["isReminderOn"] = True
            else:
                body["isReminderOn"] = False
        return body, ""

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        body, problem = self._fields(call.inputs)
        if problem:
            return invalid(problem)
        if body.get("dueDateTime", 1) is None:
            body.pop("dueDateTime")
        try:
            list_id = self._list_id(client, call.inputs.get("list_id"))
            created = client.create_task(list_id, body)
        except GraphError as exc:
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
            return invalid("Nothing to change: give a title, due, importance, "
                           "note or reminder.")
        return self._patch(client, call, body)

    async def complete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        # Graph stamps completedDateTime itself when the status turns.
        return self._patch(client, call, {"status": "completed"})

    async def reopen(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        return self._patch(client, call, {"status": "notStarted"})

    @staticmethod
    def _patch(client, call, body):
        list_id, task_id = str(call.inputs["list_id"]), str(call.inputs["task_id"])
        try:
            updated = client.update_task(list_id, task_id, body)
            if not updated:
                updated = client.get_task(list_id, task_id)
        except GraphError as exc:
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
        except GraphError as exc:
            return failure(exc)
        return {"deleted": True, "task_id": task_id,
                "title": str(task.get("title") or "")}, "success"
