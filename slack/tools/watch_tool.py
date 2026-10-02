"""The watch: what a schedule asks of this Slack account.

A watch remembers how far the person's mentions and direct messages
have been read — a cursor that is a Slack ts, in Slack's own clock.
new() returns what arrived since, oldest first, and moves the cursor
to the last message it handed on; its ``messages`` list is what a
schedule wakes the assistant on, so a quiet check costs no model call.

Two sources, because Slack has no single feed of "things for me":

- mentions come from search.messages for <@USERID>, sorted newest
  first and filtered here to those after the cursor;
- direct and group-direct messages come from each such conversation's
  history after the cursor. There can be hundreds of those, so a check
  reads a bounded number, most recently touched first, and says how
  many it left unread.
"""

import time

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, not_connected
from .slack_api import (People, SlackError, conversation_name, is_said,
                        message_row, ts_number, ts_time)
from .messages_tool import search_row

WHICH = ("mentions", "dms", "both")
# DM and group-DM conversations read per check. A schedule runs every
# few minutes, and each conversation is one conversations.history call.
CONVERSATIONS_PER_CHECK = 40
SEARCH_PAGES = 3


class WatchTool(ToolBase):
    id = "watch"

    async def start(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        which = str(call.inputs.get("which") or "both")
        skip = self._ids(call.inputs.get("skip_conversations"))
        if which not in WHICH:
            return {"error": "which is mentions, dms or both.", "kind": "invalid"}, "error"
        if skip is None:
            return {"error": "skip_conversations is Slack conversation ids such as "
                             "C0123ABCD, comma-separated.", "kind": "invalid"}, "error"
        try:
            # Confirm the connection before recording anything: a watch on
            # a revoked token would only fail later, on a clock.
            me = client.me()
        except SlackError as exc:
            return failure(exc)
        # From now. A Slack ts is seconds since the epoch, so the local
        # clock writes one in the same terms.
        cursor = f"{time.time():.6f}"
        record = await call.resources.create_data("watch", {
            "which": which, "skip_conversations": ",".join(skip), "cursor": cursor,
            "status": "watching", "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "which": which, "cursor": cursor,
                "since": ts_time(cursor), "user": str(me.get("user") or "")}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        rows, checked, more, unchecked = [], 0, False, 0
        try:
            me = client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            people = People(client)
            for watch in watches:
                keys = watch.get("keys") or {}
                if keys.get("status") != "watching":
                    continue
                checked += 1
                ref = str(watch.get("resource_ref") or "")
                cursor = str(keys.get("cursor") or "")
                which = str(keys.get("which") or "both")
                skip = set(self._ids(keys.get("skip_conversations")) or [])
                found = {}
                if which in ("mentions", "both"):
                    for row in self._mentions(client, people, me_id, cursor, skip):
                        found[(row["conversation_id"], row["ts"])] = row
                if which in ("dms", "both"):
                    dms, left = self._direct(client, people, me_id, team_url, cursor, skip)
                    unchecked += left
                    for row in dms:
                        # A mention inside a DM is still a DM.
                        found[(row["conversation_id"], row["ts"])] = row
                news = sorted(found.values(), key=lambda r: ts_number(r["ts"]))
                if len(news) > most:
                    more = True
                    news = news[:most]
                rows.extend({**row, "watch_ref": ref} for row in news)
                if news:
                    # Only through what was handed on; the rest is next time's.
                    await call.resources.update_data("watch", ref, {"cursor": news[-1]["ts"]})
        except SlackError as exc:
            return failure(exc)
        return {"checked": checked, "messages": rows, "more": more,
                "unchecked": unchecked}, "success"

    # -- the two sources -----------------------------------------------------

    @staticmethod
    def _mentions(client, people, me_id, cursor, skip):
        after = ts_number(cursor)
        rows = []
        for page in range(1, SEARCH_PAGES + 1):
            found = client.search(f"<@{me_id}>", 100, page)
            matches = found.get("matches") or []
            for match in matches:
                channel = str((match.get("channel") or {}).get("id") or "")
                if (ts_number(match.get("ts")) <= after or channel in skip
                        or str(match.get("user") or "") == me_id):
                    continue
                rows.append({**search_row(match, people), "kind": "mention"})
            # Newest first: once a page reaches the cursor, older pages
            # hold nothing new.
            reached = any(ts_number(m.get("ts")) <= after for m in matches)
            pages = int((found.get("paging") or {}).get("pages") or 1)
            if reached or page >= pages:
                break
        return rows

    @staticmethod
    def _direct(client, people, me_id, team_url, cursor, skip):
        conversations = [c for c in client.my_conversations("im,mpim")
                         if str(c.get("id") or "") not in skip]
        # users.conversations has no "last message" field; updated (or,
        # failing that, created) is the best hint at recent activity.
        conversations.sort(key=lambda c: int(c.get("updated") or 0)
                           or int(c.get("created") or 0) * 1000, reverse=True)
        left = max(0, len(conversations) - CONVERSATIONS_PER_CHECK)
        rows = []
        for conversation in conversations[:CONVERSATIONS_PER_CHECK]:
            channel = str(conversation.get("id") or "")
            messages = [m for m in client.history_since(channel, cursor, pages=2)
                        if is_said(m) and str(m.get("user") or "") != me_id]
            if not messages:
                continue
            name = conversation_name(conversation, people, client, me_id)
            for message in messages:
                row = message_row(message, channel, people, me_id, team_url)
                rows.append({"conversation_id": channel, "conversation": name,
                             "kind": "dm", "ts": row["ts"], "thread_ts": row["thread_ts"],
                             "sent": row["sent"], "author": row["author"],
                             "author_id": row["author_id"], "text": row["text"],
                             "link": row["link"]})
        return rows, left

    @staticmethod
    def _ids(text):
        """'C0123, D0456' → ['C0123', 'D0456']; None when one is not an id."""
        ids = [part.strip() for part in str(text or "").split(",") if part.strip()]
        if any(not part.isalnum() or not part.isupper() for part in ids):
            return None
        return ids
