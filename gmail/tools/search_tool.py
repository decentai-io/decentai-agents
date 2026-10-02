import base64

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .gmail import GmailError, body_and_attachments, header, message_link, summary_row

#: The platform's upload limit, and the file slots the manifest
#: declares. Bytes past the wire's line travel through the worker's
#: spool now, so the wire no longer caps a file; what the platform
#: will store does. Refused here, with its size, before the bytes
#: are asked for.
MAX_BYTES = 25 * 1024 * 1024


class SearchTool(ToolBase):
    id = "search"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        query = str(call.inputs["query"])
        limit = int(call.inputs.get("max_results") or 10)
        try:
            page = client.list_messages(
                query, limit, str(call.inputs.get("page_token") or ""))
            rows = []
            for stub in page.get("messages") or []:
                rows.append(summary_row(client.get_message(str(stub["id"]))))
        except GmailError as exc:
            return failure(exc)
        result = {"messages": rows,
                  "total_estimate": int(page.get("resultSizeEstimate") or len(rows))}
        if page.get("nextPageToken"):
            result["next_page_token"] = str(page["nextPageToken"])
        return result, "success"

    async def thread(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        cap = int(call.inputs.get("max_body_chars") or 4000)
        try:
            thread = client.get_thread(str(call.inputs["thread_id"]))
        except GmailError as exc:
            return failure(exc)
        messages, attachments = [], []
        for message in thread.get("messages") or []:
            text, attached = body_and_attachments(message)
            message_id = str(message.get("id") or "")
            messages.append({
                "message_id": message_id,
                "internet_id": header(message, "Message-ID"),
                "from": header(message, "From"),
                "to": header(message, "To"),
                "cc": header(message, "Cc"),
                "date": header(message, "Date"),
                "subject": header(message, "Subject"),
                "body": text[:cap],
                "truncated": len(text) > cap,
                "link": message_link(message_id),
            })
            attachments.extend({"message_id": message_id, **item}
                               for item in attached)
        subject = messages[0]["subject"] if messages else ""
        return {"thread_id": str(thread.get("id") or call.inputs["thread_id"]),
                "subject": subject, "messages": messages,
                "attachments": attachments}, "success"

    async def save_attachment(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        filename = str(call.inputs["filename"])
        try:
            raw = client.get_attachment(str(call.inputs["message_id"]),
                                        str(call.inputs["attachment_id"]))
        except GmailError as exc:
            return failure(exc)
        if not raw:
            return {"error": "Gmail returned an empty attachment."}, "error"
        if len(raw) > MAX_BYTES:
            return {"error": f"“{filename}” is {len(raw) // 1024:,} KB, and this "
                             f"platform can carry at most {MAX_BYTES // 1024:,} KB. "
                             f"It was not saved. Nothing is wrong with the message — "
                             f"the file is simply too large to hand to another agent.",
                    "kind": "too_large", "size": len(raw),
                    "limit": MAX_BYTES}, "error"
        await call.progress(f"Saving {filename} ({len(raw)} bytes)")
        saved = await call.resources.create_file(
            "attachment", filename,
            content_base64=base64.b64encode(raw).decode("ascii"))
        return {"file_ref": saved["resource_ref"], "filename": filename,
                "size": len(raw)}, "success"
