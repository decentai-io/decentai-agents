"""A Word document's comments, read from the file's word/comments.xml.

Graph offers no comments API for Word files, so this reads what the
file holds and nothing more: the agent does not add, answer or resolve
comments."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, open_document
from .graph_word import GraphError

MAX_TEXT = 2000


def cut(text, most=MAX_TEXT) -> str:
    text = str(text or "")
    return text if len(text) <= most else text[:most] + "…"


class CommentsTool(ToolBase):
    id = "comments"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        start = int(call.inputs.get("from") or 1)
        most = int(call.inputs.get("max_results") or 25)
        try:
            item, package = open_document(client, item_id)
        except GraphError as exc:
            return failure(exc)
        found = package.comments()
        rows = [{
            "comment_id": c["comment_id"], "author": c["author"], "date": c["date"],
            "text": cut(c["text"]), "paragraph": c["paragraph"],
            "paragraph_text": cut(c["paragraph_text"], 300),
        } for c in found[start - 1: start - 1 + most]]
        result = {"item_id": item_id, "name": str(item.get("name") or ""),
                  "etag": str(item.get("eTag") or ""), "total": len(found),
                  "from": start, "comments": rows}
        if start - 1 + len(rows) < len(found):
            result["next_from"] = start + len(rows)
        return result, "success"
