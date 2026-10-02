"""Watches: the two things a schedule can ask this mailbox.

A REPLY watch remembers where a conversation stood; check() reads the
conversation again and asks whether anyone else has written since. Its
``unanswered`` list is what a schedule wakes the assistant on — empty
once the reply came, so a quiet check costs no model call.

An INBOX watch remembers how far the inbox has been read, in the mail
server's own count: every message in a folder has a number that only
grows, so "what came after number n" loses nothing and shows nothing
twice. new_mail() returns what arrived since, oldest first, and moves
the mark. Senders can be narrowed here so a newsletter costs no model
call either.
"""

from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from . import messages
from .account_tool import account_for, failure
from .mailbox import Mailbox
from .search_tool import conversation, day
from .servers import MailError


class WatchTool(ToolBase):
    id = "watch"

    async def await_reply(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            with Mailbox(account) as mailbox:
                found = conversation(mailbox, str(call.inputs["thread_id"]))
        except MailError as exc:
            return failure(exc)
        subject = messages.header(found[0][0], "Subject")
        last = messages.message_id(found[-1][0])
        thread_id = messages.thread_id(found[-1][0])
        record = await call.resources.create_data("watch", {
            "thread_id": thread_id, "subject": subject,
            "since_message_id": last, "status": "waiting",
            "account": account.email.lower(),
            "note": str(call.inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "subject": subject,
                "thread_id": thread_id, "since_message_id": last}, "success"

    async def check(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "waiting"})
        accounts = {}
        unanswered, answered = [], []
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "waiting":
                continue
            account, why = await self._account_of(call, keys, accounts)
            if account is None:
                return {"error": why, "kind": "auth"}, "error"
            ref = str(watch.get("resource_ref") or "")
            thread_id = str(keys.get("thread_id") or "")
            try:
                with Mailbox(account) as mailbox:
                    found = conversation(mailbox, thread_id)
            except MailError as exc:
                return failure(exc)
            reply = self._reply_since(
                found, str(keys.get("since_message_id") or ""),
                account.email.lower())
            entry = {"watch_ref": ref, "thread_id": thread_id,
                     "subject": str(keys.get("subject") or "")}
            if reply is None:
                unanswered.append({**entry, "note": str(keys.get("note") or "")})
            else:
                await call.resources.update_data("watch", ref, {"status": "replied"})
                answered.append({
                    **entry, "reply_from": messages.header(reply[0], "From"),
                    "reply_date": messages.sent_at(*reply),
                    "message_id": messages.message_id(reply[0])})
        return {"checked": len(unanswered) + len(answered),
                "unanswered": unanswered, "answered": answered}, "success"

    @staticmethod
    async def _account_of(call, keys, accounts):
        """The account a watch was made on — asked for once, however
        many watches share it."""
        named = str(keys.get("account") or "").lower()
        if named not in accounts:
            accounts[named] = await account_for(call, account=named)
        return accounts[named]

    @staticmethod
    def _reply_since(found, since_message_id, me):
        """The first message after the watch began that is not ours."""
        seen = False
        for message, received in found:
            if not seen:
                seen = messages.message_id(message) == since_message_id
                continue
            if messages.address(message.get("From")) != me:
                return message, received
        return None

    # -- the inbox -------------------------------------------------------

    async def inbox(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        asked = str(call.inputs.get("since") or "").strip()
        since, note = None, ""
        if asked:
            try:
                since = datetime.fromisoformat(asked.replace("Z", "+00:00"))
            except ValueError:
                return {"error": f"since must be an ISO 8601 date-time, "
                                 f"not {asked!r}.", "kind": "invalid"}, "error"
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            # A start in the future is a time written in the person's
            # own zone and read as UTC. Nobody means to start watching
            # later tonight; it is taken as "from now", and said.
            if since > datetime.now(timezone.utc):
                note = (f"since {asked!r} is in the future, so the watch starts "
                        f"from now. Give a time with its offset, like "
                        f"2026-09-20T17:51:00+04:00.")
                since = None
        try:
            with Mailbox(account) as mailbox:
                validity, following = mailbox.open(Mailbox.INBOX)
                last = following - 1
                if since is not None:
                    last = self._before(mailbox, since, last)
        except MailError as exc:
            return failure(exc)
        started = (since or datetime.now(timezone.utc)).astimezone(
            timezone.utc).isoformat(timespec="seconds")
        record = await call.resources.create_data("inbox", {
            "since": started, "folder_validity": str(validity),
            "last_number": str(max(last, 0)), "status": "watching",
            "account": account.email.lower(),
            "only_from": self._senders(call.inputs.get("only_from")),
            "skip_from": self._senders(call.inputs.get("skip_from")),
            "note": str(call.inputs.get("note") or ""),
        })
        result = {"inbox_ref": record["resource_ref"], "since": started}
        if note:
            result["note"] = note
        return result, "success"

    @staticmethod
    def _before(mailbox, since, newest):
        """The number just before the first message that arrived at or
        after ``since``. A mail server searches by the day; the hour is
        read from each message it names."""
        for uid in mailbox.search(["SINCE", day(since.isoformat())]):
            message, received, _ = mailbox.glance(uid)
            arrived = messages.moment(messages.received_at(received)
                                      or messages.sent_at(message))
            if arrived >= since:
                return uid - 1
        return newest

    async def new_mail(self, call):
        wanted = str(call.inputs.get("inbox_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("inbox", wanted)]
        else:
            watches = await call.resources.list_data("inbox", {"status": "watching"})
        accounts = {}
        rows, checked, more = [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            account, why = await self._account_of(call, keys, accounts)
            if account is None:
                return {"error": why, "kind": "auth"}, "error"
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            me = account.email.lower()
            try:
                with Mailbox(account) as mailbox:
                    validity, following = mailbox.open(Mailbox.INBOX)
                    if str(validity) != str(keys.get("folder_validity") or ""):
                        # The server numbered the folder afresh: what
                        # was remembered points at nothing. The watch
                        # starts again from now, and says so.
                        await call.resources.update_data("inbox", ref, {
                            "folder_validity": str(validity),
                            "last_number": str(max(following - 1, 0)),
                            "note": "The mail server renumbered the inbox; "
                                    "the watch started again from that moment."})
                        continue
                    last = int(str(keys.get("last_number") or "0") or 0)
                    arrived = mailbox.after(last)
                    if len(arrived) > most:
                        more = True
                        arrived = arrived[:most]
                    for uid in arrived:
                        message, received, _ = mailbox.glance(uid)
                        sender = messages.address(message.get("From"))
                        if sender == me or not self._wanted(sender, keys):
                            continue
                        rows.append({**messages.row(message, received),
                                     "inbox_ref": ref})
            except MailError as exc:
                return failure(exc)
            if arrived:
                await call.resources.update_data("inbox", ref, {
                    "last_number": str(arrived[-1]),
                    "since": datetime.now(timezone.utc).isoformat(timespec="seconds")})
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
