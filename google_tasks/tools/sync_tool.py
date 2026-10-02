"""Watches: what a schedule can ask of these lists.

A watch remembers how far one list (or every list) has been read, as
two cursors on one record, each a moment in Google's own clock (to the
second) plus the ids that sit exactly on that second, so a second task
in the same second is neither lost nor shown twice:

- the completion cursor follows a task's ``completed`` time, asked for
  with ``completedMin`` — so editing a task that was already ticked off
  does not report it again;
- the change cursor follows its ``updated`` time, asked for with
  ``updatedMin``.

completed() and changed() return what is new in one list field and move
their own cursor; that list is what a schedule wakes the assistant on,
so a quiet check costs no model call. Google cannot be asked for an
order, so everything since the cursor is read (a bounded number of
pages) and sorted here; ``more`` is exact.
"""

from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .google_api import GoogleError, second
from .tasks_tool import task_row

#: Which task time each cursor follows, and how Google is asked for it.
#: Completed tasks are asked for with showHidden too: one ticked off in
#: Google's own apps is hidden as well as completed.
KINDS = {
    "completed": ("completed", "completedMin"),
    "changed": ("updated", "updatedMin"),
}


class SyncTool(ToolBase):
    id = "sync"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        list_id = str(call.inputs.get("list_id") or "").strip()
        asked = str(call.inputs.get("since") or "").strip()
        if asked and not second(asked):
            return invalid(f"since must be an ISO 8601 date-time, not {asked!r}.")
        try:
            lists = self._lists(client, list_id)
            if asked:
                cursors = {kind: (second(asked), "") for kind in KINDS}
            else:
                cursors = self._start(client, lists)
        except GoogleError as exc:
            return failure(exc)
        list_name = lists[0][1] if list_id else ""
        record = await call.resources.create_data("watch", {
            "list_id": list_id, "list_name": list_name,
            "completed_since": cursors["completed"][0], "completed_ids": cursors["completed"][1],
            "changed_since": cursors["changed"][0], "changed_ids": cursors["changed"][1],
            "status": "watching", "note": str(call.inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "list_id": list_id,
                "list_name": list_name or "(every list)",
                "since": cursors["changed"][0]}, "success"

    async def completed(self, call):
        return await self._check(call, "completed")

    async def changed(self, call):
        return await self._check(call, "changed")

    # -- the check -------------------------------------------------------
    async def _check(self, call, kind):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        field, parameter = KINDS[kind]
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        rows, checked, more = [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            since = str(keys.get(f"{kind}_since") or "")
            seen = {i for i in str(keys.get(f"{kind}_ids") or "").split(",") if i}
            try:
                lists = self._lists(client, str(keys.get("list_id") or ""))
                fetched = []
                for list_id, list_name in lists:
                    for task in client.all_tasks(list_id, **{
                            parameter: since, "showCompleted": "true", "showHidden": "true"}):
                        moment = second(task.get(field))
                        if not moment or task.get("deleted"):
                            continue
                        if moment == since and str(task.get("id") or "") in seen:
                            continue
                        fetched.append((moment, list_id, list_name, task))
            except GoogleError as exc:
                return failure(exc)
            # Every list in one order, oldest first; the cursor moves only
            # through what is handed on, and the rest is next time's.
            fetched.sort(key=lambda entry: entry[0])
            if len(fetched) > most:
                more = True
                fetched = fetched[:most]
            for _, list_id, list_name, task in fetched:
                if kind == "completed" and task.get("status") != "completed":
                    continue
                rows.append({**task_row(task, list_id), "list_name": list_name,
                             "watch_ref": ref})
            if fetched:
                newest = fetched[-1][0]
                at_newest = {str(entry[3].get("id") or "") for entry in fetched
                             if entry[0] == newest}
                if newest == since:
                    at_newest |= seen
                await call.resources.update_data("watch", ref, {
                    f"{kind}_since": newest, f"{kind}_ids": ",".join(sorted(at_newest))})
        return {"checked": checked, kind: rows, "more": more}, "success"

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _lists(client, list_id):
        """[(id, name)] — the one list watched, or every list."""
        if list_id:
            row = client.get_list(list_id)
            return [(str(row.get("id") or list_id), str(row.get("title") or ""))]
        return [(str(r.get("id") or ""), str(r.get("title") or ""))
                for r in client.all_lists()]

    @staticmethod
    def _start(client, lists):
        """Where each cursor starts: the newest completion and the newest
        change in the lists, in Google's clock rather than this process's,
        with every task on that second — none of them is news. Recording
        a watch is done once, so reading the lists whole for it is
        affordable. With no completion yet, completions start at the
        newest change (a completion is a change too); an empty account
        starts from this moment."""
        tasks = []
        for list_id, _ in lists:
            tasks.extend(client.all_tasks(list_id, showCompleted="true", showHidden="true"))
        cursors = {}
        for kind, (field, _) in KINDS.items():
            moments = [(second(t.get(field)), str(t.get("id") or "")) for t in tasks
                       if second(t.get(field))]
            if moments:
                newest = max(m for m, _ in moments)
                cursors[kind] = (newest, ",".join(sorted(i for m, i in moments if m == newest)))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        changed = cursors.get("changed", (now, ""))
        return {"changed": changed, "completed": cursors.get("completed", (changed[0], ""))}
