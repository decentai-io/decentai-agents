"""Finding pages and databases by title.

Notion's search matches titles only, and only across what was shared
with the integration — which is exactly what the agent can open, so a
page missing here is a page to share, not a page that does not exist.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .notion_api import NotionError
from .schema import PageRow
from .text import PropertyText, Text


class SearchTool(ToolBase):
    id = "search"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 20)
        try:
            answer = client.search(str(call.inputs.get("query") or "").strip(),
                                   str(call.inputs.get("kind") or "any"), most,
                                   str(call.inputs.get("start_cursor") or ""))
        except NotionError as exc:
            return failure(exc)
        rows = []
        for obj in (answer.get("results") or [])[:most]:
            kind = str(obj.get("object") or "")
            rows.append({"id": str(obj.get("id") or ""), "kind": kind,
                         "title": Text.clip(PropertyText.title(obj), 300),
                         "url": str(obj.get("url") or ""),
                         "last_edited": str(obj.get("last_edited_time") or ""),
                         "parent": PageRow.parent(obj)})
        result = {"results": rows}
        if answer.get("has_more") and answer.get("next_cursor"):
            result["next_cursor"] = str(answer["next_cursor"])
        return result, "success"
