"""Watches: the two things a schedule can ask this mailbox.

A REPLY watch remembers where a conversation stood; check() asks
Outlook whether anyone else has written since, and its ``unanswered``
list is what a schedule wakes the assistant on — empty once the reply
came, so a quiet check costs no model call.

An INBOX watch remembers how far the inbox has been read — a timestamp
cursor in Graph's own clock, plus the ids that sit exactly on it, so a
second mail in the same second is neither lost nor shown twice.
new_mail() returns what arrived since and moves the cursor; its
``messages`` list is what a schedule wakes the assistant on. Senders
can be narrowed here so a newsletter costs no model call either."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph import GraphError, address, bare_address, graph_time, summary_row


class WatchTool(ToolBase):
    id = "watch"

    async def await_reply(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        thread_id = str(call.inputs["thread_id"])
        try:
            messages = client.get_thread(thread_id)
        except GraphError as exc:
            return failure(exc)
        if not messages:
            return {"error": f"Conversation {thread_id} has no messages.",
                    "kind": "not_found"}, "error"
        last = messages[-1]
        subject = str(messages[0].get("subject") or "")
        record = await call.resources.create_data("watch", {
            "thread_id": thread_id, "subject": subject,
            "since_message_id": str(last.get("id") or ""),
            "status": "waiting", "account": client.email.lower(),
            "note": str(call.inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "subject": subject,
                "since_message_id": str(last.get("id") or "")}, "success"

    async def check(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "waiting"})
        clients = {}
        unanswered, answered = [], []
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "waiting":
                continue
            client, why = await self._client_for_watch(call, keys, clients)
            if client is None:
                return {"error": why, "kind": "auth"}, "error"
            me = client.email.lower()
            ref = str(watch.get("resource_ref") or "")
            thread_id = str(keys.get("thread_id") or "")
            try:
                messages = client.get_thread(thread_id)
            except GraphError as exc:
                return failure(exc)
            reply = self._reply_since(messages, str(keys.get("since_message_id") or ""), me)
            entry = {"watch_ref": ref, "thread_id": thread_id,
                     "subject": str(keys.get("subject") or "")}
            if reply is None:
                unanswered.append({**entry, "note": str(keys.get("note") or "")})
            else:
                await call.resources.update_data("watch", ref, {"status": "replied"})
                answered.append({**entry, "reply_from": address(reply.get("from")),
                                 "reply_date": str(reply.get("receivedDateTime") or ""),
                                 "message_id": str(reply.get("id") or "")})
        return {"checked": len(unanswered) + len(answered),
                "unanswered": unanswered, "answered": answered}, "success"

    @staticmethod
    async def _client_for_watch(call, keys, clients):
        """The client for the account a watch was made on — one per
        account, however many watches share it. A watch recorded before
        accounts were remembered follows the default."""
        account = str(keys.get("account") or "").lower()
        if account not in clients:
            clients[account] = await client_for(call, account=account)
        return clients[account]

    @staticmethod
    def _reply_since(messages, since_message_id, me):
        """The first message after the watch began that is not ours."""
        seen = False
        for message in messages:
            message_id = str(message.get("id") or "")
            if not seen:
                seen = message_id == since_message_id
                continue
            if bare_address(message.get("from")) != me:
                return message
        return None

    # -- the inbox -------------------------------------------------------

    async def inbox(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        asked = str(call.inputs.get("since") or "").strip()
        cursor_ids, since, note = "", "", ""
        if asked:
            since = graph_time(asked)
            if not since:
                return {"error": f"since must be an ISO 8601 date-time, "
                                 f"not {asked!r}.", "kind": "invalid"}, "error"
            # A start in the future is a time written in the person's own
            # zone and read in the mailbox's: "17:51" meant Dubai and was
            # taken as UTC, so the watch began four hours ahead and every
            # check until then honestly found nothing. Nobody means to
            # start watching later tonight; it is taken as "from now", and
            # said.
            from datetime import datetime, timezone
            if since > datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"):
                note = (f"since {asked!r} is in the future by the mailbox's clock "
                        f"(UTC), so the watch starts from now. Give a time with "
                        f"its offset, like 2026-09-20T17:51:00+04:00.")
                since = ""
        if not since:
            # From now — and "now" is the mailbox's, not this process's:
            # the newest message marks the spot and is not news itself.
            try:
                newest = client.newest_inbox_message()
            except GraphError as exc:
                return failure(exc)
            if newest is not None:
                since = str(newest.get("receivedDateTime") or "")
                cursor_ids = str(newest.get("id") or "")
            else:
                from datetime import datetime, timezone
                since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = await call.resources.create_data("inbox", {
            "since": since, "cursor_ids": cursor_ids, "status": "watching",
            "account": client.email.lower(),
            "only_from": self._senders(call.inputs.get("only_from")),
            "skip_from": self._senders(call.inputs.get("skip_from")),
            "note": str(call.inputs.get("note") or ""),
        })
        started = {"inbox_ref": record["resource_ref"], "since": since}
        if note:
            started["note"] = note
        return started, "success"

    async def new_mail(self, call):
        wanted = str(call.inputs.get("inbox_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("inbox", wanted)]
        else:
            watches = await call.resources.list_data("inbox", {"status": "watching"})
        clients = {}
        rows, checked, more = [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            client, why = await self._client_for_watch(call, keys, clients)
            if client is None:
                return {"error": why, "kind": "auth"}, "error"
            me = client.email.lower()
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            since = str(keys.get("since") or "")
            seen = {i for i in str(keys.get("cursor_ids") or "").split(",") if i}
            try:
                # One more than asked for, so "more" is known without
                # reading it; the cursor moves only through what was
                # read, and the rest is next time's.
                fetched = client.inbox_since(since, most + 1)
            except GraphError as exc:
                return failure(exc)
            if len(fetched) > most:
                more = True
                fetched = fetched[:most]
            for message in fetched:
                if str(message.get("id") or "") in seen:
                    continue
                sender = bare_address(message.get("from"))
                if sender == me or not self._wanted(sender, keys):
                    continue
                rows.append({**summary_row(message), "inbox_ref": ref})
            if fetched:
                newest = str(fetched[-1].get("receivedDateTime") or "")
                at_newest = {str(m.get("id") or "") for m in fetched
                             if str(m.get("receivedDateTime") or "") == newest}
                if newest == since:
                    at_newest |= seen
                await call.resources.update_data("inbox", ref, {
                    "since": newest, "cursor_ids": ",".join(sorted(at_newest))})
        return {"checked": checked, "messages": rows, "more": more}, "success"

    @staticmethod
    def _senders(text) -> str:
        """'Dana@x.example, @harbourline.example' → lower-case, tidy."""
        return ",".join(part.strip().lower()
                        for part in str(text or "").split(",") if part.strip())

    @classmethod
    def _wanted(cls, sender: str, keys: dict) -> bool:
        only = [p for p in str(keys.get("only_from") or "").split(",") if p]
        skip = [p for p in str(keys.get("skip_from") or "").split(",") if p]
        if any(cls._sender_is(sender, p) for p in skip):
            return False
        return not only or any(cls._sender_is(sender, p) for p in only)

    @staticmethod
    def _sender_is(sender: str, pattern: str) -> bool:
        """An address matches itself; '@domain' matches the domain."""
        if pattern.startswith("@"):
            return sender.endswith(pattern)
        return sender == pattern
