"""Watches: what changed in the Box account since the last look.

A WATCH remembers how far the account's change stream has been read —
Box's own stream position, taken when the watch began. new() reads the
events since that position, hands on the ones about files and folders,
and moves the position; its ``changes`` list is what a schedule wakes
the assistant on, so a quiet check costs no model call.

Box says plainly that it may deliver an event more than once, so the
watch also remembers the ids of the events it handed on most recently
and never hands one on twice. Box reads a chunk at a time and moves the
position past the whole chunk, so a chunk is never cut short here; when
a full chunk came back there may be more, and ``more`` says so.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .box_api import BoxError, folder_path

#: The events that are about the files and folders themselves, as Box
#: names them, and the word a row uses for each.
CHANGES = {
    "ITEM_UPLOAD": "uploaded",
    "ITEM_CREATE": "created",
    "ITEM_MOVE": "moved",
    "ITEM_COPY": "copied",
    "ITEM_RENAME": "renamed",
    "ITEM_TRASH": "trashed",
    "ITEM_UNDELETE_VIA_TRASH": "restored",
    "ITEM_MAKE_CURRENT_VERSION": "version_restored",
    "ITEM_SHARED_CREATE": "link_created",
    "ITEM_SHARED_UPDATE": "link_changed",
    "ITEM_SHARED_UNSHARE": "link_removed",
    "COLLAB_ADD_COLLABORATOR": "collaborator_added",
    "COLLAB_REMOVE_COLLABORATOR": "collaborator_removed",
}
#: Event ids remembered per watch against a second delivery.
REMEMBERED = 100
#: Stop visiting further watches once this many changes are in hand.
MOST = 25


class ChangesTool(ToolBase):
    id = "changes"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            position = str(client.events("now", 1).get("next_stream_position") or "")
        except BoxError as exc:
            return failure(exc)
        if not position:
            return {"error": "Box gave no stream position to watch from."}, "error"
        record = await call.resources.create_data("watch", {
            "cursor": position, "seen_ids": "", "status": "watching",
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "since": position}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        wanted = str(call.inputs.get("watch_ref") or "")
        chunk = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        rows, checked, more = [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            if len(rows) >= MOST:
                more = True
                break
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            seen = [i for i in str(keys.get("seen_ids") or "").split(",") if i]
            try:
                answer = client.events(str(keys.get("cursor") or ""), chunk)
            except BoxError as exc:
                return failure(exc)
            entries = answer.get("entries") or []
            # Oldest first; Box's order is not a promise, its clock is.
            for event in sorted(entries, key=lambda e: str(e.get("created_at") or "")):
                event_id = str(event.get("event_id") or "")
                if not event_id or event_id in seen:
                    continue
                seen.append(event_id)
                row = self._row(event)
                if row is not None:
                    rows.append({**row, "watch_ref": ref})
            # The position moves when the chunk is handed on, so a wake the
            # assistant failed to act on is not offered again.
            await call.resources.update_data("watch", ref, {
                "cursor": str(answer.get("next_stream_position") or keys.get("cursor") or ""),
                "seen_ids": ",".join(seen[-REMEMBERED:])})
            more = more or len(entries) >= chunk
        return {"checked": checked, "changes": rows, "more": more}, "success"

    @staticmethod
    def _row(event):
        change = CHANGES.get(str(event.get("event_type") or ""))
        source = event.get("source") or {}
        if source.get("type") == "collaboration":
            source = source.get("item") or {}
        if change is None or source.get("type") not in ("file", "folder"):
            return None
        name = str(source.get("name") or "")
        folder = folder_path(source) if source.get("path_collection") else ""
        return {"change": change, "event_id": str(event.get("event_id") or ""),
                "item_id": str(source.get("id") or ""), "name": name,
                "kind": str(source.get("type")), "folder": folder,
                "path": (folder.rstrip("/") + "/" + name) if folder else "",
                "size": int(source.get("size") or 0),
                "modified": str(event.get("created_at") or ""),
                "modified_by": str((event.get("created_by") or {}).get("login") or "")}
