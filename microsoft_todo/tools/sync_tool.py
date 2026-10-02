"""Watches: what a schedule can ask of these lists.

A watch remembers how far one list (or every list) has been read, as
two cursors on one record — one for tasks ticked off, one for any
change. Each cursor is a moment in Graph's own clock (a task's
lastModifiedDateTime, to the second) plus the ids that sit exactly on
that second, so a second task changed in the same second is neither
lost nor shown twice.

completed() and changed() return what is new in one list field and move
their own cursor; that list is what a schedule wakes the assistant on,
so a quiet check costs no model call. Each fetches one more than asked
for, so ``more`` is known without reading it.
"""

from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .graph_todo import GraphError, second
from .tasks_tool import task_row


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
                since, ids = second(asked), ""
            else:
                since, ids = self._newest(client, lists)
        except GraphError as exc:
            return failure(exc)
        list_name = lists[0][1] if list_id else ""
        record = await call.resources.create_data("watch", {
            "list_id": list_id, "list_name": list_name,
            "completed_since": since, "completed_ids": ids,
            "changed_since": since, "changed_ids": ids,
            "status": "watching", "note": str(call.inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "list_id": list_id,
                "list_name": list_name or "(every list)", "since": since}, "success"

    async def completed(self, call):
        return await self._check(call, "completed")

    async def changed(self, call):
        return await self._check(call, "changed")

    # -- the check -------------------------------------------------------
    async def _check(self, call, kind):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
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
                    # The ids already handed on at the cursor's second come
                    # back too; ask for that many more so they cannot crowd
                    # out what is new.
                    for task in client.changed_since(list_id, since, most + 1 + len(seen),
                                                     completed_only=(kind == "completed")):
                        moment = second(task.get("lastModifiedDateTime"))
                        if moment == since and str(task.get("id") or "") in seen:
                            continue
                        fetched.append((moment, list_id, list_name, task))
            except GraphError as exc:
                return failure(exc)
            # Every list in one order, oldest change first; the cursor moves
            # only through what was read, and the rest is next time's.
            fetched.sort(key=lambda entry: entry[0])
            if len(fetched) > most:
                more = True
                fetched = fetched[:most]
            for _, list_id, list_name, task in fetched:
                # The filter asked for completed tasks; completedDateTime is
                # Microsoft's own word that it was ticked off.
                if kind == "completed" and not (task.get("status") == "completed"
                                                and task.get("completedDateTime")):
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
            return [(list_id, str(row.get("displayName") or ""))]
        return [(str(r.get("id") or ""), str(r.get("displayName") or ""))
                for r in client.all_lists()]

    @staticmethod
    def _newest(client, lists):
        """Where a watch starts: the newest change in the lists, in Graph's
        clock, and every task on that second — none of them is news. An
        empty account starts from this moment."""
        moments = []
        for list_id, _ in lists:
            for task in client.newest(list_id):
                moments.append((second(task.get("lastModifiedDateTime")),
                                str(task.get("id") or "")))
        if not moments:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), ""
        newest = max(m for m, _ in moments)
        return newest, ",".join(sorted(i for m, i in moments if m == newest))
