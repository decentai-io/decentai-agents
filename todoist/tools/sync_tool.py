"""Watching what was ticked off in Todoist, so a schedule can carry it
elsewhere (the platform's Tasks agent, a report) without anyone asking.

A WATCH remembers how far completions have been handed on: the
completion time of the newest completed task seen, in Todoist's own
clock, plus the task ids that sit exactly on that time — so a second
task completed in the same instant is neither lost nor shown twice.
completed() returns what was completed since and moves the cursor; its
``completed`` list is what a schedule wakes the assistant on, so a
quiet check costs no model call.
"""

from datetime import datetime, timedelta, timezone

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .catalog import Catalog, task_row
from .todoist_api import TodoistError

#: Todoist answers at most three months of completions per request.
WINDOW = timedelta(days=89)
#: How far back a new watch looks for the newest completion to start from.
LOOKBACK = timedelta(days=1)


def parse_time(text: str):
    """Todoist's '2026-09-13T09:00:00.000000Z', or None."""
    text = str(text or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def wire_time(moment: datetime) -> str:
    """Whole seconds, UTC — how the range is asked for. Rounded down, so
    the range still includes the cursor itself; the ids on it decide."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SyncTool(ToolBase):
    id = "sync"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        project_id = project_name = ""
        try:
            if inputs.get("project"):
                project, problem = Catalog(client).project(inputs["project"])
                if problem:
                    return problem, "error"
                project_id, project_name = str(project["id"]), str(project.get("name") or "")
            asked = str(inputs.get("since") or "").strip()
            cursor_ids = ""
            if asked:
                moment = parse_time(asked)
                if moment is None:
                    return {"error": f"since must be an ISO 8601 date-time, not {asked!r}.",
                            "kind": "invalid"}, "error"
                since = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            else:
                # From now — and "now" is Todoist's: the newest recent
                # completion marks the spot and is not news itself.
                now = datetime.now(timezone.utc)
                recent = client.completed(wire_time(now - LOOKBACK),
                                          wire_time(now + timedelta(seconds=1)), project_id)
                timed = [(parse_time(t.get("completed_at")), t) for t in recent]
                timed = [(m, t) for m, t in timed if m is not None]
                if timed:
                    newest = max(m for m, _ in timed)
                    at_newest = [t for m, t in timed if m == newest]
                    since = str(at_newest[0].get("completed_at"))
                    cursor_ids = ",".join(sorted(str(t.get("id")) for t in at_newest))
                else:
                    since = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except TodoistError as exc:
            return failure(exc)
        record = await call.resources.create_data("watch", {
            "since": since, "cursor_ids": cursor_ids, "status": "watching",
            "project_id": project_id, "project": project_name,
            "note": str(inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "since": since,
                "project": project_name}, "success"

    async def completed(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        catalog = Catalog(client)
        rows, checked, more = [], 0, False
        now = datetime.now(timezone.utc)
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            since_text = str(keys.get("since") or "")
            since = parse_time(since_text) or now
            seen = {i for i in str(keys.get("cursor_ids") or "").split(",") if i}
            # A watch left alone for months is read three months at a time;
            # otherwise up to a second past now, because the range is
            # asked in whole seconds and this second's completions count.
            capped = since + WINDOW < now
            until = since + WINDOW if capped else max(now + timedelta(seconds=1), since)
            try:
                items = client.completed(wire_time(since), wire_time(until),
                                         str(keys.get("project_id") or ""))
            except TodoistError as exc:
                return failure(exc)
            fresh = []
            for task in items:
                moment = parse_time(task.get("completed_at"))
                if moment is None or moment < since:
                    continue
                if moment == since and str(task.get("id")) in seen:
                    continue
                fresh.append((moment, str(task.get("id")), task))
            fresh.sort(key=lambda entry: (entry[0], entry[1]))
            # The cursor moves only through what is handed on; the rest
            # is next time's, and "more" says so.
            if len(fresh) > most:
                more = True
                fresh = fresh[:most]
            if fresh:
                try:
                    names = catalog.project_names()
                except TodoistError as exc:
                    return failure(exc)
                for moment, _, task in fresh:
                    rows.append({**task_row(task, names),
                                 "completed_at": str(task.get("completed_at") or ""),
                                 "watch_ref": ref})
                newest = fresh[-1][0]
                at_newest = {task_id for moment, task_id, _ in fresh if moment == newest}
                if newest == since:
                    at_newest |= seen
                await call.resources.update_data("watch", ref, {
                    "since": str(fresh[-1][2].get("completed_at")),
                    "cursor_ids": ",".join(sorted(at_newest))})
            elif capped:
                # Nothing in this three-month stretch: step past it.
                more = True
                await call.resources.update_data("watch", ref, {
                    "since": wire_time(until), "cursor_ids": ""})
            if capped and fresh:
                more = True
        return {"checked": checked, "completed": rows, "more": more}, "success"
