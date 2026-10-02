"""Finding Word documents in OneDrive and reading one as numbered
paragraphs, a slice at a time, with the file's eTag — the version the
edit tool checks before it saves."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, open_document
from .graph_word import GraphError, is_docx, person


class DocsTool(ToolBase):
    id = "docs"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 10)
        try:
            # Graph's search has no newest-first order and no file-type
            # filter, so a wider page is sorted and narrowed here.
            items = client.search(str(call.inputs["query"]), 50)
        except GraphError as exc:
            return failure(exc)
        documents = sorted((i for i in items if is_docx(i)),
                           key=lambda i: str(i.get("lastModifiedDateTime") or ""), reverse=True)
        return {"documents": [{
            "item_id": str(i.get("id") or ""), "name": str(i.get("name") or ""),
            "modified": str(i.get("lastModifiedDateTime") or ""),
            "modified_by": person(i.get("lastModifiedBy")),
            "size": int(i.get("size") or 0), "link": str(i.get("webUrl") or ""),
        } for i in documents[:most]]}, "success"

    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        start = int(call.inputs.get("from") or 1)
        most = int(call.inputs.get("max_paragraphs") or 50)
        budget = int(call.inputs.get("max_chars") or 20000)
        try:
            item, package = open_document(client, item_id)
        except GraphError as exc:
            return failure(exc)
        total = len(package.paragraphs)
        rows, used = [], 0
        for number in range(start, total + 1):
            row = package.row(number)
            if len(rows) >= most or (rows and used + len(row["text"]) > budget):
                break
            if len(row["text"]) > budget:
                # One paragraph longer than the whole budget is still
                # shown, cut, and says so — never silently skipped.
                row["text"], row["truncated"] = row["text"][:budget], True
            rows.append(row)
            used += len(row["text"])
        result = {"item_id": item_id, "name": str(item.get("name") or ""),
                  "etag": str(item.get("eTag") or ""),
                  "modified": str(item.get("lastModifiedDateTime") or ""),
                  "modified_by": person(item.get("lastModifiedBy")),
                  "total_paragraphs": total, "from": start, "paragraphs": rows,
                  "comment_count": len(package.comments()),
                  "link": str(item.get("webUrl") or "")}
        if start + len(rows) <= total:
            result["next_from"] = start + len(rows)
        return result, "success"
