"""Finding Google Docs and reading one as numbered paragraphs.

A document is handed to the model a slice at a time: paragraphs from
``from``, at most ``max_paragraphs`` of them and at most ``max_chars``
of text, with the total so the assistant knows how far it has read.
The paragraph numbers are the ones edits.propose takes.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .docs_api import GoogleError, doc_link, paragraphs, person


class DocsTool(ToolBase):
    id = "docs"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 10)
        try:
            files = client.find_documents(str(call.inputs["query"]), most)
        except GoogleError as exc:
            return failure(exc)
        return {"documents": [{
            "document_id": str(f.get("id") or ""),
            "title": str(f.get("name") or ""),
            "modified": str(f.get("modifiedTime") or ""),
            "modified_by": person(f.get("lastModifyingUser")),
            "link": str(f.get("webViewLink") or doc_link(str(f.get("id") or ""))),
        } for f in files[:most]]}, "success"

    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        document_id = str(call.inputs["document_id"])
        start = int(call.inputs.get("from") or 1)
        most = int(call.inputs.get("max_paragraphs") or 50)
        budget = int(call.inputs.get("max_chars") or 20000)
        try:
            document = client.document(document_id)
        except GoogleError as exc:
            return failure(exc)
        found = paragraphs(document)
        rows, used = [], 0
        for paragraph in found[start - 1:]:
            if len(rows) >= most or (rows and used + len(paragraph.text) > budget):
                break
            row = paragraph.row()
            if len(row["text"]) > budget:
                # One paragraph longer than the whole budget is still
                # shown, cut, and says so — never silently skipped.
                row["text"], row["truncated"] = row["text"][:budget], True
            rows.append(row)
            used += len(row["text"])
        result = {"document_id": str(document.get("documentId") or document_id),
                  "title": str(document.get("title") or ""),
                  "revision_id": str(document.get("revisionId") or ""),
                  "total_paragraphs": len(found), "from": start,
                  "paragraphs": rows, "link": doc_link(document_id)}
        after = start + len(rows)
        if after <= len(found):
            result["next_from"] = after
        return result, "success"
