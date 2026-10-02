"""Adding to the end of a page: paragraphs, to-dos and bullets.

Only plain kinds are offered. A heading or a table written by the agent
into a page others keep would be a layout decision, which is the
person's to make in Notion.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .notion_api import NotionError
from .schema import PageRow
from .text import Text

KINDS = {"paragraph": "paragraph", "to_do": "to_do", "bullet": "bulleted_list_item"}


class BlocksTool(ToolBase):
    id = "blocks"

    @staticmethod
    def _block(item):
        kind = KINDS[str(item.get("type") or "paragraph")]
        body = {"rich_text": Text.write(str(item.get("text") or ""))}
        if kind == "to_do":
            body["checked"] = bool(item.get("checked"))
        return {"object": "block", "type": kind, kind: body}

    async def append(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        page_id = str(call.inputs["page_id"]).strip()
        items = [i for i in call.inputs["items"] if str(i.get("text") or "").strip()]
        if not items:
            return invalid("Every item was empty; nothing to add.")
        try:
            page = client.page(page_id)
            if page.get("archived") or page.get("in_trash"):
                return invalid("That page is in the Notion trash; restore it in Notion first.")
            answer = client.append(page_id, [self._block(i) for i in items])
        except NotionError as exc:
            return failure(exc)
        row = PageRow.row(page, with_properties=False)
        added = answer.get("results") or []
        record = await call.resources.create_data("write", {
            "action": "blocks_appended", "page_id": page_id, "title": row["title"],
            "parent_id": PageRow.parent_id(page), "link": row["url"],
            "changed": f"{len(items)} blocks",
            "detail": {"items": [{"type": str(i.get("type") or "paragraph"),
                                  "text": Text.clip(str(i.get("text") or ""), 500)}
                                 for i in items]}})
        return {"page_id": page_id, "title": row["title"], "url": row["url"],
                "appended": len(items),
                "block_ids": [str(b.get("id") or "") for b in added][-len(items):],
                "write_ref": record["resource_ref"]}, "success"
