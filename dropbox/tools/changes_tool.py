"""Watches: what changed in a folder since the last look.

A WATCH remembers how far a folder (and everything under it) has been
read — Dropbox's own list_folder cursor, taken when the watch began.
new() asks Dropbox what changed since that cursor, hands the changes on
and moves the cursor; its ``changes`` list is what a schedule wakes the
assistant on, so a quiet check costs no model call.

The cursor is Dropbox's, so nothing is lost or shown twice between
checks: Dropbox answers a cursor with what changed after it, a page at
a time, and says whether there is more. The page size is fixed when the
cursor is made, which is why a watch reads one page per check rather
than taking a number here. Dropbox may declare a cursor stale
("reset"); the watch then starts again from now and says so in
``reset``, because whatever changed in between was not seen.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .dropbox_api import DropboxError, clean_path, item_row, parent_of, tag

#: Changes per page, fixed into the cursor. Dropbox treats it as
#: approximate, so a page may hold a few more.
PAGE = 25
#: Stop visiting further watches once this many changes are in hand;
#: the rest keep their cursors for the next check.
MOST = 25


class ChangesTool(ToolBase):
    id = "changes"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        folder_id = str(call.inputs.get("folder_id") or "")
        folder = clean_path(call.inputs.get("folder_path") or "/")
        try:
            if folder_id or folder != "/":
                found = client.metadata(folder_id or folder)
                if tag(found) != "folder":
                    return {"error": f"'{folder_id or folder}' is a file, not a folder; "
                                     f"watch the folder it is in."}, "error"
                folder = str(found.get("path_display") or folder)
                folder_id = str(found.get("id") or folder_id)
            cursor = client.latest_cursor(folder_id or folder, PAGE)
        except DropboxError as exc:
            if exc.kind == "not_found":
                return {"error": f"Dropbox has no folder at '{folder_id or folder}'.",
                        "kind": "not_found"}, "error"
            return failure(exc)
        record = await call.resources.create_data("watch", {
            "folder": folder, "folder_id": folder_id, "cursor": cursor,
            "status": "watching", "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "folder": folder}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        rows, reset, checked, more = [], [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            if len(rows) >= MOST:
                more = True
                break
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            folder = str(keys.get("folder") or "/")
            try:
                page = client.list_folder_continue(str(keys.get("cursor") or ""))
            except DropboxError as exc:
                if "reset" not in exc.summary:
                    return failure(exc)
                try:
                    cursor = client.latest_cursor(str(keys.get("folder_id") or folder), PAGE)
                except DropboxError as again:
                    return failure(again)
                await call.resources.update_data("watch", ref, {"cursor": cursor})
                reset.append({"watch_ref": ref, "folder": folder,
                              "note": "Dropbox reset this watch; it starts again from "
                                      "now, and changes since the last check were not "
                                      "seen. List the folder to catch up."})
                continue
            for entry in page.get("entries") or []:
                row = self._row(client, entry)
                if row is not None:
                    rows.append({**row, "watch_ref": ref})
            # The cursor moves when the page is handed on, so a wake the
            # assistant failed to act on is not offered again.
            await call.resources.update_data("watch", ref, {
                "cursor": str(page.get("cursor") or keys.get("cursor") or "")})
            more = more or bool(page.get("has_more"))
        return {"checked": checked, "changes": rows, "reset": reset,
                "more": more}, "success"

    @staticmethod
    def _row(client, entry):
        """A file added or changed, or anything deleted. Dropbox does not
        say whether a file is new or edited — both arrive as the file as
        it now is — and a new folder alone is not news."""
        kind = tag(entry)
        path = str(entry.get("path_display") or entry.get("path_lower") or "")
        if kind == "deleted":
            return {"change": "deleted", "item_id": "", "name": str(entry.get("name") or ""),
                    "path": path, "folder": parent_of(path), "size": 0,
                    "modified": "", "modified_by": ""}
        if kind != "file":
            return None
        row = item_row(entry, client)
        return {"change": "added_or_changed", "item_id": row["item_id"], "name": row["name"],
                "path": path, "folder": row["folder"], "size": row["size"],
                "modified": row["modified"], "modified_by": row["modified_by"]}
