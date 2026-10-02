"""Drafts: prepared in Outlook, recorded on the platform, sent only on a
separate level-3 call whose success is Outlook's word alone.

Graph's send answers 202 and nothing more, so confirmation is a read
of the same message afterwards: with immutable ids the draft keeps its
id on its way to Sent Items, and ``isDraft`` turning false is the
provider saying it went."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph import GraphError, address, addresses, bare_address, recipients


class DraftTool(ToolBase):
    id = "draft"

    async def reply(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        thread_id = str(call.inputs["thread_id"])
        body = str(call.inputs["body"])
        try:
            messages = client.get_thread(thread_id)
            if not messages:
                raise GraphError("not_found", f"Outlook has no conversation {thread_id}.")
            last = messages[-1]
            # Outlook addresses the reply to the last sender; when the
            # last message was ours, it would address us, so the
            # original recipients are used instead — unless told whom.
            to = str(call.inputs.get("to") or "").strip()
            if not to and bare_address(last.get("from")) == client.email.lower():
                to = addresses(last.get("toRecipients"))
            if not to and not address(last.get("from")):
                return {"error": "The thread names no one to reply to; give "
                                 "\"to\" explicitly."}, "error"
            draft = client.create_reply(str(last.get("id") or ""))
            changes = {"body": {"contentType": "text", "content": body}}
            if to:
                changes["toRecipients"] = recipients(to)
            cc = str(call.inputs.get("cc") or "")
            if cc:
                changes["ccRecipients"] = recipients(cc)
            draft = client.update_draft(str(draft.get("id") or ""), changes) or draft
        except GraphError as exc:
            return failure(exc)
        to = to or addresses(draft.get("toRecipients"))
        subject = str(draft.get("subject") or "")
        return await self._record(call, client, draft, "reply", to, subject, thread_id, body)

    async def compose(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        to = str(call.inputs["to"]).strip()
        subject = str(call.inputs["subject"]).strip()
        body = str(call.inputs["body"])
        try:
            draft = client.create_draft(to, subject, body,
                                        cc=str(call.inputs.get("cc") or ""))
        except GraphError as exc:
            return failure(exc)
        return await self._record(call, client, draft, "new", to, subject,
                                  str(draft.get("conversationId") or ""), body)

    async def _record(self, call, client, draft, kind, to, subject, thread_id, body):
        draft_id = str(draft.get("id") or "")
        if not draft_id:
            return {"error": "Outlook created no draft."}, "error"
        fields = {
            "outlook_draft_id": draft_id, "kind": kind, "to": to,
            "subject": subject, "status": "prepared",
            # The account it was made on: sending and discarding follow
            # it, whatever the default is by then.
            "account": client.email.lower(),
            "body": {"text": body},
        }
        if thread_id:
            fields["thread_id"] = thread_id
        record = await call.resources.create_data("draft", fields)
        result = {"draft_ref": record["resource_ref"],
                  "outlook_draft_id": draft_id, "to": to, "subject": subject}
        if draft.get("webLink"):
            result["link"] = str(draft["webLink"])
        return result, "success"

    async def send(self, call):
        draft_ref = str(call.inputs["draft_ref"])
        record = await call.resources.read_data("draft", draft_ref)
        keys = record.get("keys") or {}
        client, why = await client_for(call, account=str(keys.get("account") or ""))
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        status = str(keys.get("status") or "")
        if status == "sent":
            return {"error": "This draft was already sent "
                             f"(message {keys.get('sent_message_id')}).",
                    "kind": "http"}, "error"
        if status == "unknown":
            return {"error": "This draft's last send had an unknown outcome. "
                             "Check the Sent folder before sending again; "
                             "this function will not retry it.",
                    "kind": "unknown"}, "error"
        if status == "discarded":
            return {"error": "This draft was discarded."}, "error"
        draft_id = str(keys.get("outlook_draft_id") or "")
        await call.progress(f"Sending to {keys.get('to')}: {keys.get('subject')}")
        try:
            client.send_draft(draft_id)
        except GraphError as exc:
            if exc.kind == "unknown":
                # Outlook may or may not have sent it. Recorded as such,
                # so nothing here or later sends it twice by accident.
                await call.resources.update_data("draft", draft_ref,
                                                 {"status": "unknown"})
                return {"error": exc.message, "kind": "unknown"}, "error"
            return failure(exc)
        # 202 means "accepted", not "sent": Outlook moves the message
        # in the background, and a read a moment too early still shows
        # a draft. Read it back for a few seconds before calling the
        # outcome unknown.
        sent = self._confirm(client, draft_id)
        if sent.get("isDraft") is not False:
            await call.resources.update_data("draft", draft_ref,
                                             {"status": "unknown"})
            return {"error": "Outlook accepted the send but has not confirmed "
                             "it; the outcome is unknown.",
                    "kind": "unknown"}, "error"
        message_id = str(sent.get("id") or draft_id)
        await call.resources.update_data("draft", draft_ref, {
            "status": "sent", "sent_message_id": message_id})
        return {"sent": True, "status": "sent", "message_id": message_id,
                "thread_id": str(sent.get("conversationId") or ""),
                "link": str(sent.get("webLink") or "")}, "success"

    #: How long to wait for Outlook to say the draft left: a handful of
    #: reads a half-second apart, which covers the ordinary case and
    #: still answers a stuck one within the function's timeout.
    CONFIRM_ATTEMPTS = 8
    CONFIRM_PAUSE = 0.5

    def _confirm(self, client, draft_id):
        import time

        last = {}
        for attempt in range(self.CONFIRM_ATTEMPTS):
            try:
                last = client.get_message(draft_id)
            except GraphError:
                last = {}
            if last.get("isDraft") is False:
                return last
            if attempt < self.CONFIRM_ATTEMPTS - 1:
                time.sleep(self.CONFIRM_PAUSE)
        return last

    async def discard(self, call):
        draft_ref = str(call.inputs["draft_ref"])
        record = await call.resources.read_data("draft", draft_ref)
        keys = record.get("keys") or {}
        client, why = await client_for(call, account=str(keys.get("account") or ""))
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        if keys.get("status") == "sent":
            return {"error": "A sent message cannot be discarded."}, "error"
        try:
            client.delete_draft(str(keys.get("outlook_draft_id") or ""))
        except GraphError as exc:
            if exc.kind != "not_found":
                return failure(exc)
        await call.resources.update_data("draft", draft_ref,
                                         {"status": "discarded"})
        return {"discarded": True}, "success"
