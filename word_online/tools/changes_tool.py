"""Watches: whether a Word document changed, and what was commented.

A WATCH remembers the file as it last was: its eTag, when it was last
modified, and a fingerprint of every comment it held. changes.new asks
Graph for the item only — a few hundred bytes — and downloads the file
only when the eTag moved. Then it says who saved it and when, and which
comments are new. Its ``changes`` list is what a schedule wakes the
assistant on, so a quiet check costs no model call.

A save by the account itself is not news unless it carried someone
else's new comment; a comment whose author is the account's own name
is not news either. Word writes a comment's author as a display name,
not an address, so that is the name compared.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, open_document
from .comments_tool import cut
from .graph_word import GraphError, person

MAX_ROWS = 25
MAX_KEYS_TEXT = 8000   # a record key holds at most 8,192 characters


def _keys_text(package) -> str:
    # 17 characters a comment: some 470 comments fit. Past that the
    # oldest fall out of the cursor and would be reported once more.
    return ",".join(c["key"] for c in package.comments())[-MAX_KEYS_TEXT:].lstrip(",")


class ChangesTool(ToolBase):
    id = "changes"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        try:
            item, package = open_document(client, item_id)
        except GraphError as exc:
            return failure(exc)
        name = str(item.get("name") or "")
        modified = str(item.get("lastModifiedDateTime") or "")
        record = await call.resources.create_data("watch", {
            "item_id": item_id, "name": name, "modified": modified,
            "etag": str(item.get("eTag") or ""), "comment_keys": _keys_text(package),
            "status": "watching", "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "name": name, "modified": modified,
                "comment_count": len(package.comments())}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        me = None
        changes, comments, checked, unshown, more = [], [], 0, 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            if len(changes) >= MAX_ROWS:
                # The rest keep their cursors and are next check's.
                more = True
                break
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            item_id = str(keys.get("item_id") or "")
            try:
                item = client.item(item_id)
                if str(item.get("eTag") or "") == str(keys.get("etag") or ""):
                    continue
                if me is None:
                    me = client.profile()
                _, package = open_document(client, item_id)
            except GraphError as exc:
                return failure(exc)
            seen = set(str(keys.get("comment_keys") or "").split(","))
            own_name = str(me.get("displayName") or "").strip().lower()
            fresh = [c for c in package.comments() if c["key"] not in seen
                     and (not own_name or c["author"].strip().lower() != own_name)]
            modifier = (((item.get("lastModifiedBy") or {}).get("user") or {}).get("email") or "")
            own_addresses = {str(me.get(k) or "").lower() for k in ("mail", "userPrincipalName")}
            by_me = modifier.lower() in own_addresses - {""}
            if fresh or not by_me:
                changes.append({
                    "watch_ref": ref, "item_id": item_id, "name": str(item.get("name") or ""),
                    "modified": str(item.get("lastModifiedDateTime") or ""),
                    "modified_by": person(item.get("lastModifiedBy")),
                    "new_comments": len(fresh), "link": str(item.get("webUrl") or "")})
                for comment in fresh:
                    if len(comments) >= MAX_ROWS:
                        unshown += 1
                        continue
                    comments.append({
                        "watch_ref": ref, "item_id": item_id, "author": comment["author"],
                        "date": comment["date"], "text": cut(comment["text"]),
                        "paragraph": comment["paragraph"],
                        "paragraph_text": cut(comment["paragraph_text"], 300)})
            await call.resources.update_data("watch", ref, {
                "etag": str(item.get("eTag") or ""),
                "modified": str(item.get("lastModifiedDateTime") or ""),
                "comment_keys": _keys_text(package)})
        return {"checked": checked, "changes": changes, "comments": comments,
                "comments_not_shown": unshown, "more": more}, "success"
