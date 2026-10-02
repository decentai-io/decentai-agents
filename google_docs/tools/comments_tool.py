"""Comments on a Google Doc, which live in Drive rather than in Docs.

Reading is free; adding, replying and resolving are seen by everyone
who can open the document, so each is level 3.

A WATCH remembers how far a document's comments have been read — the
creation time of the newest comment or reply seen (Drive's own clock),
plus the ids created exactly then, so two in the same millisecond are
neither lost nor shown twice. comments.new returns what was written
since and moves the watch; its ``comments`` list is what a schedule
wakes the assistant on, so a quiet check costs no model call. What the
account wrote itself is never news.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .docs_api import GoogleError, doc_link, paragraphs, person

MAX_TEXT = 2000
REPLIES_SHOWN = 5
MAX_PAGES = 10       # 1,000 comments; a document with more is read in later checks


def _cut(text, most=MAX_TEXT) -> str:
    text = str(text or "")
    return text if len(text) <= most else text[:most] + "…"


def _quoted(comment) -> str:
    return _cut((comment.get("quotedFileContent") or {}).get("value"), 500)


class CommentsTool(ToolBase):
    id = "comments"

    # -- reads -----------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        document_id = str(call.inputs["document_id"])
        include_resolved = call.inputs.get("include_resolved", True) is not False
        try:
            answer = client.comments(document_id, int(call.inputs.get("max_results") or 10),
                                     str(call.inputs.get("page_token") or ""))
        except GoogleError as exc:
            return failure(exc)
        comments, replies = [], []
        for comment in answer.get("comments") or []:
            if comment.get("resolved") and not include_resolved:
                continue
            thread = [r for r in comment.get("replies") or [] if not r.get("deleted")]
            comment_id = str(comment.get("id") or "")
            comments.append({
                "comment_id": comment_id, "author": person(comment.get("author")),
                "created": str(comment.get("createdTime") or ""),
                "modified": str(comment.get("modifiedTime") or ""),
                "text": _cut(comment.get("content")), "quoted": _quoted(comment),
                "resolved": bool(comment.get("resolved")), "reply_count": len(thread)})
            # The latest few of each thread; reply_count says how many there are.
            for reply in thread[-REPLIES_SHOWN:]:
                replies.append({
                    "comment_id": comment_id, "reply_id": str(reply.get("id") or ""),
                    "author": person(reply.get("author")),
                    "created": str(reply.get("createdTime") or ""),
                    "text": _cut(reply.get("content")),
                    "action": str(reply.get("action") or "")})
        result = {"comments": comments, "replies": replies}
        if answer.get("nextPageToken"):
            result["next_page_token"] = str(answer["nextPageToken"])
        return result, "success"

    # -- writes others see ---------------------------------------------
    async def add(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        document_id = str(call.inputs["document_id"])
        quote = str(call.inputs.get("quote") or "").strip()
        try:
            if quote:
                # A quote is shown to readers as the passage the comment is
                # about, so it must be words the document actually holds.
                text = "\n".join(p.text for p in paragraphs(client.document(document_id)))
                if quote not in text:
                    return {"error": "The document does not contain that quote; copy "
                                     "it exactly from docs.read.", "kind": "invalid"}, "error"
            made = client.add_comment(document_id, str(call.inputs["text"]), quote)
        except GoogleError as exc:
            return failure(exc)
        return {"comment_id": str(made.get("id") or ""),
                "created": str(made.get("createdTime") or ""),
                "quoted": quote, "link": doc_link(document_id)}, "success"

    async def reply(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            made = client.add_reply(str(call.inputs["document_id"]),
                                    str(call.inputs["comment_id"]), str(call.inputs["text"]))
        except GoogleError as exc:
            return failure(exc)
        return {"reply_id": str(made.get("id") or ""),
                "created": str(made.get("createdTime") or "")}, "success"

    async def resolve(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        document_id = str(call.inputs["document_id"])
        comment_id = str(call.inputs["comment_id"])
        try:
            if client.comment(document_id, comment_id).get("resolved"):
                return {"resolved": True, "reply_id": "",
                        "note": "It was already resolved."}, "success"
            # Drive resolves a comment by a reply carrying the action.
            made = client.add_reply(document_id, comment_id,
                                    str(call.inputs.get("text") or ""), action="resolve")
        except GoogleError as exc:
            return failure(exc)
        return {"resolved": True, "reply_id": str(made.get("id") or "")}, "success"

    # -- watching --------------------------------------------------------
    @staticmethod
    def _all_comments(client, document_id, since=""):
        found, token = [], ""
        for _ in range(MAX_PAGES):
            answer = client.comments(document_id, 100, token, since)
            found.extend(answer.get("comments") or [])
            token = str(answer.get("nextPageToken") or "")
            if not token:
                break
        return found

    @staticmethod
    def _events(comments):
        """Every comment and reply as one list, oldest first: (created,
        seen id, comment, reply or None)."""
        events = []
        for comment in comments:
            comment_id = str(comment.get("id") or "")
            events.append((str(comment.get("createdTime") or ""), comment_id, comment, None))
            for reply in comment.get("replies") or []:
                if reply.get("deleted"):
                    continue
                events.append((str(reply.get("createdTime") or ""),
                               f"{comment_id}/{reply.get('id') or ''}", comment, reply))
        return sorted(events, key=lambda e: (e[0], e[1]))

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        document_id = str(call.inputs["document_id"])
        try:
            info = client.file(document_id)
            events = self._events(self._all_comments(client, document_id))
        except GoogleError as exc:
            return failure(exc)
        if events:
            since = events[-1][0]
            cursor_ids = ",".join(e[1] for e in events if e[0] == since)
        else:
            # No comment yet: the document's last change is a moment every
            # future comment comes after, in Google's clock, not this one's.
            since, cursor_ids = str(info.get("modifiedTime") or ""), ""
        title = str(info.get("name") or "")
        record = await call.resources.create_data("watch", {
            "document_id": document_id, "title": title, "since": since,
            "cursor_ids": cursor_ids, "status": "watching",
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "title": title, "since": since}, "success"

    async def new(self, call):
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
            document_id = str(keys.get("document_id") or "")
            since = str(keys.get("since") or "")
            seen = {i for i in str(keys.get("cursor_ids") or "").split(",") if i}
            try:
                # Drive's filter is on MODIFIED time, which a new reply
                # also moves; which items are new is decided here, by
                # creation time.
                comments = self._all_comments(client, document_id, since)
            except GoogleError as exc:
                return failure(exc)
            fresh = [e for e in self._events(comments)
                     if e[0] > since or (e[0] == since and e[1] not in seen)]
            if len(fresh) > most:
                more = True
                fresh = fresh[:most]
            for created, _, comment, reply in fresh:
                item = reply or comment
                if (item.get("author") or {}).get("me"):
                    continue
                rows.append({
                    "watch_ref": ref, "document_id": document_id,
                    "title": str(keys.get("title") or ""),
                    "kind": "reply" if reply else "comment",
                    "comment_id": str(comment.get("id") or ""),
                    "reply_id": str(reply.get("id") or "") if reply else "",
                    "author": person(item.get("author")), "created": created,
                    "text": _cut(item.get("content")), "quoted": _quoted(comment),
                    "in_reply_to": _cut(comment.get("content"), 300) if reply else "",
                    "link": doc_link(document_id)})
            if fresh:
                newest = fresh[-1][0]
                at_newest = {e[1] for e in fresh if e[0] == newest}
                if newest == since:
                    at_newest |= seen
                await call.resources.update_data("watch", ref, {
                    "since": newest, "cursor_ids": ",".join(sorted(at_newest))})
        return {"checked": checked, "comments": rows, "more": more}, "success"
