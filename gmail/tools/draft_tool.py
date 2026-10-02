"""Drafts: prepared in Gmail, recorded on the platform, sent only on a
separate level-3 call whose success is Gmail's word alone."""

from email.message import EmailMessage
from email.utils import parseaddr

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .gmail import GmailError, header, message_link


def _mime(sender: str, to: str, subject: str, body: str, cc: str = "",
          in_reply_to: str = "", references: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = (references + " " + in_reply_to).strip()
    message.set_content(body)
    return message.as_bytes()


def _reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class DraftTool(ToolBase):
    id = "draft"

    async def reply(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        thread_id = str(call.inputs["thread_id"])
        try:
            thread = client.get_thread(thread_id)
        except GmailError as exc:
            return failure(exc)
        messages = thread.get("messages") or []
        if not messages:
            return {"error": f"Thread {thread_id} has no messages."}, "error"
        last = messages[-1]
        # Reply to the last sender unless told otherwise; never to
        # ourselves when the last message was ours.
        to = str(call.inputs.get("to") or "").strip()
        if not to:
            sender = header(last, "From")
            if parseaddr(sender)[1].lower() == client.email.lower():
                to = header(last, "To")
            else:
                to = sender
        if not to:
            return {"error": "The thread names no one to reply to; give "
                             "\"to\" explicitly."}, "error"
        subject = _reply_subject(header(last, "Subject"))
        raw = _mime(client.email, to, subject, str(call.inputs["body"]),
                    cc=str(call.inputs.get("cc") or ""),
                    in_reply_to=header(last, "Message-ID"),
                    references=header(last, "References"))
        return await self._record(call, client, raw, "reply", to, subject,
                                  thread_id)

    async def compose(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        to = str(call.inputs["to"]).strip()
        subject = str(call.inputs["subject"]).strip()
        raw = _mime(client.email, to, subject, str(call.inputs["body"]),
                    cc=str(call.inputs.get("cc") or ""))
        return await self._record(call, client, raw, "new", to, subject, "")

    async def _record(self, call, client, raw, kind, to, subject, thread_id):
        try:
            draft = client.create_draft(raw, thread_id)
        except GmailError as exc:
            return failure(exc)
        gmail_draft_id = str(draft.get("id") or "")
        if not gmail_draft_id:
            return {"error": "Gmail created no draft."}, "error"
        fields = {
            "gmail_draft_id": gmail_draft_id, "kind": kind, "to": to,
            "subject": subject, "status": "prepared",
            # The account it was made on: sending and discarding follow
            # it, whatever the default is by then.
            "account": client.email.lower(),
            "body": {"text": str(call.inputs["body"])},
        }
        if thread_id:
            fields["thread_id"] = thread_id
        record = await call.resources.create_data("draft", fields)
        result = {"draft_ref": record["resource_ref"],
                  "gmail_draft_id": gmail_draft_id, "to": to,
                  "subject": subject}
        draft_message = (draft.get("message") or {}).get("id")
        if draft_message:
            result["link"] = message_link(str(draft_message))
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
        await call.progress(f"Sending to {keys.get('to')}: {keys.get('subject')}")
        try:
            sent = client.send_draft(str(keys.get("gmail_draft_id") or ""))
        except GmailError as exc:
            if exc.kind == "unknown":
                # Gmail may or may not have sent it. Recorded as such, so
                # nothing here or later sends it twice by accident.
                await call.resources.update_data("draft", draft_ref,
                                                 {"status": "unknown"})
                return {"error": exc.message, "kind": "unknown"}, "error"
            return failure(exc)
        message_id = str(sent.get("id") or "")
        if not message_id:
            await call.resources.update_data("draft", draft_ref,
                                             {"status": "unknown"})
            return {"error": "Gmail accepted the send but returned no "
                             "message id; the outcome is unknown.",
                    "kind": "unknown"}, "error"
        await call.resources.update_data("draft", draft_ref, {
            "status": "sent", "sent_message_id": message_id})
        return {"sent": True, "status": "sent", "message_id": message_id,
                "thread_id": str(sent.get("threadId") or ""),
                "link": message_link(message_id)}, "success"

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
            client.delete_draft(str(keys.get("gmail_draft_id") or ""))
        except GmailError as exc:
            if exc.kind != "not_found":
                return failure(exc)
        await call.resources.update_data("draft", draft_ref,
                                         {"status": "discarded"})
        return {"discarded": True}, "success"
