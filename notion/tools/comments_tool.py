"""Page-level comments: reading the discussion, and adding to it.

Notion names a comment's author by user id only, and resolving each id
is another request per comment, so rows carry the id as it came. A
comment added here is written by the integration, as Notion shows it.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .notion_api import NotionError
from .schema import PageRow
from .text import BLOCK_CHARS, Text


class CommentsTool(ToolBase):
    id = "comments"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 20)
        try:
            answer = client.comments(str(call.inputs["page_id"]), most,
                                     str(call.inputs.get("start_cursor") or ""))
        except NotionError as exc:
            return failure(exc)
        rows = [{"comment_id": str(c.get("id") or ""),
                 "discussion_id": str(c.get("discussion_id") or ""),
                 "author_id": str((c.get("created_by") or {}).get("id") or ""),
                 "created": str(c.get("created_time") or ""),
                 "text": Text.clip(Text.rich(c.get("rich_text")), BLOCK_CHARS)}
                for c in (answer.get("results") or [])[:most]]
        result = {"comments": rows}
        if answer.get("has_more") and answer.get("next_cursor"):
            result["next_cursor"] = str(answer["next_cursor"])
        return result, "success"

    async def add(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        page_id = str(call.inputs["page_id"]).strip()
        text = str(call.inputs["text"]).strip()
        try:
            page = client.page(page_id)
            made = client.add_comment(page_id, Text.write(text))
        except NotionError as exc:
            return failure(exc)
        row = PageRow.row(page, with_properties=False)
        record = await call.resources.create_data("write", {
            "action": "comment_added", "page_id": page_id, "title": row["title"],
            "parent_id": PageRow.parent_id(page), "link": row["url"],
            "changed": "comment", "detail": {"text": Text.clip(text, 500)}})
        return {"comment_id": str(made.get("id") or ""),
                "discussion_id": str(made.get("discussion_id") or ""),
                "page_id": page_id, "write_ref": record["resource_ref"]}, "success"
