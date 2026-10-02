"""Watching a database for new and changed rows.

A WATCH remembers how far a database has been read: a cursor in
Notion's own clock (the newest last_edited_time handed on) and, for
the rows that sit exactly on that cursor, the time each was seen at.
changes() asks for rows edited on or after the cursor, hands on the
ones not already seen, and moves the cursor; its ``rows`` list is what
a schedule wakes the assistant on.

Why the map, and not just a timestamp: Notion keeps last_edited_time to
the MINUTE. Many rows share one value, and asking "on or after" the
cursor returns the cursor's own minute again every time. Each row is
therefore reported only when its time is newer than the time recorded
for it. The honest limit is the same granularity: a row edited again
within the minute it was last reported still carries that minute, and
looks unchanged until an edit in a later minute.
"""

import json
from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .notion_api import PAGE_SIZE_MAX, NotionError
from .schema import DatabaseSchema, PageRow
from .text import Text

#: How many pages of results one check reads before it stops and says
#: "more"; a busy database cannot turn one check into a long crawl.
PAGES_PER_CHECK = 5


class WatchTool(ToolBase):
    id = "watch"

    async def database(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        database_id = str(call.inputs["database_id"]).strip()
        try:
            schema = DatabaseSchema(client.database(database_id))
            since, seen = self._newest(client, database_id)
        except NotionError as exc:
            return failure(exc)
        record = await call.resources.create_data("watch", {
            "database_id": database_id, "database": schema.title, "since": since,
            "seen": json.dumps(seen, sort_keys=True), "status": "watching",
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "database": schema.title,
                "since": since}, "success"

    @staticmethod
    def _newest(client, database_id):
        """From now, in the database's clock: the newest edit marks the
        spot, and every row on that minute counts as already seen."""
        sorts = [{"timestamp": "last_edited_time", "direction": "descending"}]
        since, seen, cursor = "", {}, ""
        for _ in range(PAGES_PER_CHECK):
            answer = client.query(database_id, PAGE_SIZE_MAX, cursor, sorts=sorts)
            for page in answer.get("results") or []:
                stamp = Text.minute(page.get("last_edited_time"))
                since = since or stamp
                if stamp != since:
                    return since, seen
                seen[str(page.get("id") or "")] = stamp
            if not answer.get("has_more"):
                break
            cursor = str(answer.get("next_cursor") or "")
        if not since:
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")
        return since, seen

    async def changes(self, call):
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
            since = str(keys.get("since") or "")
            try:
                seen = json.loads(str(keys.get("seen") or "{}"))
            except ValueError:
                seen = {}
            try:
                found, cut = self._unseen(client, str(keys.get("database_id") or ""),
                                          since, seen, most)
            except NotionError as exc:
                return failure(exc)
            more = more or cut
            for page in found:
                rows.append({**PageRow.row(page), "watch_ref": ref,
                             "database": str(keys.get("database") or ""),
                             "change": self._change(page, since, seen)})
            if found:
                await call.resources.update_data("watch", ref, self._moved(since, seen, found))
        return {"checked": checked, "rows": rows, "more": more}, "success"

    @staticmethod
    def _unseen(client, database_id, since, seen, most):
        """Rows edited on or after the cursor that were not already
        handed on at their current time, oldest first — at most ``most``,
        and whether there were more. One more than asked is looked for,
        so "more" is known without being handed on."""
        filter_ = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}}
        sorts = [{"timestamp": "last_edited_time", "direction": "ascending"}]
        found, cursor = [], ""
        for _ in range(PAGES_PER_CHECK):
            answer = client.query(database_id, PAGE_SIZE_MAX, cursor, filter_, sorts)
            for page in answer.get("results") or []:
                stamp = Text.minute(page.get("last_edited_time"))
                if stamp < since or seen.get(str(page.get("id") or ""), "") >= stamp:
                    continue
                found.append(page)
                if len(found) > most:
                    return found[:most], True
            if not answer.get("has_more"):
                return found, False
            cursor = str(answer.get("next_cursor") or "")
        return found, True

    @staticmethod
    def _change(page, since, seen):
        """created when the row came into being after the cursor — or on
        the cursor's own minute without having been seen there."""
        created = Text.minute(page.get("created_time"))
        page_id = str(page.get("id") or "")
        if created > since or (created == since and page_id not in seen):
            return "created"
        return "edited"

    @staticmethod
    def _moved(since, seen, found):
        """The cursor after handing ``found`` on: the newest minute among
        them, and the rows on that minute with the time each was seen."""
        newest = max(Text.minute(p.get("last_edited_time")) for p in found)
        kept = {k: v for k, v in seen.items() if v >= newest} if newest == since else {}
        for page in found:
            stamp = Text.minute(page.get("last_edited_time"))
            if stamp == newest:
                kept[str(page.get("id") or "")] = stamp
        return {"since": newest, "seen": json.dumps(kept, sort_keys=True)}
