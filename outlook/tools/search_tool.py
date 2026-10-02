import base64

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph import (
    GraphError, address, addresses, attachment_kind, body_text, summary_row,
)


class SearchTool(ToolBase):
    id = "search"

    #: What can actually be handed back to the platform. A saved file
    #: crosses the worker's wire as ONE JSON line of base64, which
    #: inflates the bytes by about a third, and the host refuses a line
    #: over 2 MiB. Past that the worker is killed mid-call: the person
    #: sees "Saving invoice.pdf (4238112 bytes)" and then nothing at
    #: all. Refusing here costs them a sentence instead of the agent.
    MAX_ATTACHMENT_BYTES = 700 * 1024

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        query = str(call.inputs["query"])
        limit = int(call.inputs.get("max_results") or 10)
        try:
            page = client.search_messages(
                query, limit, str(call.inputs.get("page_token") or ""))
        except GraphError as exc:
            return failure(exc)
        rows = [summary_row(message) for message in page.get("value") or []]
        result = {"messages": rows,
                  "total_estimate": int(page.get("@odata.count") or len(rows))}
        if page.get("@odata.nextLink"):
            result["next_page_token"] = str(page["@odata.nextLink"])
        return result, "success"

    async def thread(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        cap = int(call.inputs.get("max_body_chars") or 4000)
        most = int(call.inputs.get("max_messages") or 20)
        thread_id = str(call.inputs["thread_id"])
        try:
            # One more than asked for, so "there are others" can be said
            # truthfully without reading the whole conversation.
            found = client.get_thread(thread_id, limit=most + 1)
            more = len(found) > most
            found = found[:most]
            if not found:
                raise GraphError("not_found", f"Outlook has no conversation {thread_id}.")
            messages, attachments = [], []
            for message in found:
                text = body_text(message)
                message_id = str(message.get("id") or "")
                messages.append({
                    "message_id": message_id,
                    "internet_id": str(message.get("internetMessageId") or ""),
                    "from": address(message.get("from")),
                    "to": addresses(message.get("toRecipients")),
                    "cc": addresses(message.get("ccRecipients")),
                    "date": str(message.get("receivedDateTime") or message.get("sentDateTime") or ""),
                    "subject": str(message.get("subject") or ""),
                    "body": text[:cap],
                    "truncated": len(text) > cap,
                    "link": str(message.get("webLink") or ""),
                })
                if message.get("hasAttachments"):
                    for item in client.list_attachments(message_id):
                        attachments.append({
                            "message_id": message_id,
                            "attachment_id": str(item.get("id") or ""),
                            "filename": str(item.get("name") or ""),
                            "mime_type": str(item.get("contentType") or ""),
                            "size": int(item.get("size") or 0),
                        })
        except GraphError as exc:
            return failure(exc)
        return {"thread_id": thread_id, "subject": messages[0]["subject"],
                "messages": messages, "attachments": attachments,
                "message_count": len(messages),
                "more_messages": more}, "success"

    async def save_attachment(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        filename = str(call.inputs["filename"])
        message_id = str(call.inputs["message_id"])
        attachment_id = str(call.inputs["attachment_id"])
        # Ask what it is before asking for it: a 20 MB attachment pulled
        # into memory only to be refused helps nobody, and two of the
        # three kinds have no bytes to pull at all.
        try:
            info = client.attachment_info(message_id, attachment_id)
        except GraphError as exc:
            return failure(exc)
        kind = attachment_kind(info)
        if kind == "reference":
            return {"error": f"“{filename}” is a link to cloud storage, not a "
                             f"file held in the message. Open it from the "
                             f"message, or ask the sender to attach the file "
                             f"itself.", "kind": "unsupported"}, "error"
        if kind == "item":
            return {"error": f"“{filename}” is a message or calendar item "
                             f"attached to this one, not a file. Reading "
                             f"attached mail is not something this agent "
                             f"does.", "kind": "unsupported"}, "error"
        size = int(info.get("size") or 0)
        if size > self.MAX_ATTACHMENT_BYTES:
            return {"error": f"“{filename}” is {size:,} bytes, and this "
                             f"platform can carry an attachment of at most "
                             f"{self.MAX_ATTACHMENT_BYTES:,}. It was not "
                             f"saved. Nothing is wrong with the message — "
                             f"the file is simply too large to hand to "
                             f"another agent.", "kind": "too_large",
                    "size": size,
                    "limit": self.MAX_ATTACHMENT_BYTES}, "error"
        try:
            raw = client.get_attachment(message_id, attachment_id)
        except GraphError as exc:
            return failure(exc)
        if not raw:
            return {"error": f"Outlook returned no bytes for “{filename}”.",
                    "kind": "empty"}, "error"
        # Graph's reported size is the encoded size and can understate the
        # bytes; the wire cares about what is actually here.
        if len(raw) > self.MAX_ATTACHMENT_BYTES:
            return {"error": f"“{filename}” turned out to be {len(raw):,} "
                             f"bytes, over the {self.MAX_ATTACHMENT_BYTES:,} "
                             f"this platform can carry. It was not saved.",
                    "kind": "too_large", "size": len(raw),
                    "limit": self.MAX_ATTACHMENT_BYTES}, "error"
        await call.progress(f"Saving {filename} ({len(raw)} bytes)")
        saved = await call.resources.create_file(
            "attachment", filename,
            content_base64=base64.b64encode(raw).decode("ascii"))
        return {"file_ref": saved["resource_ref"], "filename": filename,
                "size": len(raw)}, "success"
