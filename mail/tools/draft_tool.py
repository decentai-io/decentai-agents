"""Drafts: kept on the platform, shown to the person, and sent only on
a separate level-3 call whose success is the mail server's word alone.

A draft is a record here and not a message in the account's Drafts
folder: what is sent is what the person was shown, letter for letter.
"""

from decentai_sdk.base import ToolBase

from . import messages
from .account_tool import account_for, failure
from .mailbox import Mailbox
from .search_tool import conversation
from .sender import Outgoing
from .servers import MailError


def reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class DraftTool(ToolBase):
    id = "draft"

    async def reply(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            with Mailbox(account) as mailbox:
                found = conversation(mailbox, str(call.inputs["thread_id"]))
        except MailError as exc:
            return failure(exc)
        mine = account.email.lower()
        last = found[-1][0]
        # Reply to the last person who wrote who is not the account
        # itself, unless told otherwise.
        to = str(call.inputs.get("to") or "").strip()
        if not to:
            for message, _ in reversed(found):
                sender = messages.address(
                    message.get("Reply-To") or message.get("From"))
                if sender and sender != mine:
                    to = sender
                    break
        if not to:
            return {"error": "Nobody but the account itself has written in this "
                             "conversation: say who the reply goes to.",
                    "kind": "invalid"}, "error"
        subject = reply_subject(messages.header(found[0][0], "Subject"))
        record = await call.resources.create_data("draft", {
            "kind": "reply",
            "thread_id": messages.thread_id(last),
            "to": to, "cc": str(call.inputs.get("cc") or ""),
            "subject": subject, "body": str(call.inputs["body"]),
            "in_reply_to": messages.message_id(last),
            "references": " ".join(
                messages.bracketed(found_id) for found_id in
                messages.ids_in(last.get("References"))),
            "status": "prepared", "account": mine,
        })
        return {"draft_ref": record["resource_ref"], "to": to,
                "subject": subject}, "success"

    async def compose(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        record = await call.resources.create_data("draft", {
            "kind": "new", "to": str(call.inputs["to"]),
            "cc": str(call.inputs.get("cc") or ""),
            "subject": str(call.inputs["subject"]),
            "body": str(call.inputs["body"]),
            "status": "prepared", "account": account.email.lower(),
        })
        return {"draft_ref": record["resource_ref"], "to": str(call.inputs["to"]),
                "subject": str(call.inputs["subject"])}, "success"

    async def send(self, call):
        ref = str(call.inputs["draft_ref"])
        record = await call.resources.read_data("draft", ref)
        keys = record.get("keys") or {}
        if keys.get("status") != "prepared":
            return {"error": f"This draft is {keys.get('status')}, not prepared: "
                             f"it is not sent again.", "kind": "invalid"}, "error"
        account, why = await account_for(call, str(keys.get("account") or ""))
        if account is None:
            return {"error": why, "kind": "auth"}, "error"
        outgoing = Outgoing(account)
        raw, message_id = outgoing.written(
            str(keys.get("to") or ""), str(keys.get("subject") or ""),
            str(keys.get("body") or ""), cc=str(keys.get("cc") or ""),
            in_reply_to=str(keys.get("in_reply_to") or ""),
            references=str(keys.get("references") or ""))
        recipients = messages.addresses(keys.get("to"), keys.get("cc"))
        thread_id = str(keys.get("thread_id") or "") or message_id
        try:
            outgoing.send(raw, recipients)
        except MailError as exc:
            if exc.kind != "unknown":
                return failure(exc)
            await call.resources.update_data("draft", ref, {
                "status": "unknown", "sent_message_id": message_id})
            return {"sent": False, "status": "unknown", "message_id": message_id,
                    "thread_id": thread_id, "problem": exc.message}, "success"
        await call.resources.update_data("draft", ref, {
            "status": "sent", "sent_message_id": message_id,
            "thread_id": thread_id})
        result = {"sent": True, "status": "sent", "message_id": message_id,
                  "thread_id": thread_id}
        # The message is sent whatever becomes of its copy.
        try:
            with Mailbox(account) as mailbox:
                result["kept_in_sent"] = mailbox.keep_sent(raw, message_id)
        except MailError as exc:
            result["kept_in_sent"] = False
            result["problem"] = (f"It was sent. A copy could not be put in the "
                                 f"Sent folder: {exc.message}")
        return result, "success"

    async def discard(self, call):
        ref = str(call.inputs["draft_ref"])
        record = await call.resources.read_data("draft", ref)
        status = (record.get("keys") or {}).get("status")
        if status != "prepared":
            return {"error": f"This draft is {status}: only a prepared draft is "
                             f"discarded.", "kind": "invalid"}, "error"
        await call.resources.update_data("draft", ref, {"status": "discarded"})
        return {"discarded": True}, "success"
