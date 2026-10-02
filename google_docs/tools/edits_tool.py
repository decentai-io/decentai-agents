"""Edits: proposed on the platform, applied to the document only on a
separate level-3 call.

A proposal records one paragraph's text as it was read, the text to
put in its place, and the document's revision at that moment. Nothing
in the document changes. Applying reads the document again and sends
the change with ``writeControl.requiredRevisionId`` set to the recorded
revision, so a document someone changed in the meantime is refused by
Google itself rather than overwritten. The paragraph's range is taken
from that fresh read, never from the proposal.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .docs_api import GoogleError, doc_link, paragraphs

MAX_KEY_TEXT = 8000   # a record key holds at most 8,192 characters


class EditsTool(ToolBase):
    id = "edits"

    async def propose(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        document_id = str(call.inputs["document_id"])
        number = int(call.inputs["paragraph"])
        new_text = str(call.inputs["new_text"])
        if "\n" in new_text or "\r" in new_text:
            return {"error": "new_text is one paragraph and cannot contain a line "
                             "break; propose each paragraph separately.",
                    "kind": "invalid"}, "error"
        try:
            document = client.document(document_id)
        except GoogleError as exc:
            return failure(exc)
        found = paragraphs(document)
        if not 1 <= number <= len(found):
            return {"error": f"The document has {len(found)} paragraphs; there is no "
                             f"paragraph {number}.", "kind": "invalid"}, "error"
        paragraph = found[number - 1]
        if not paragraph.plain:
            return {"error": f"Paragraph {number} holds an image, a chip or another "
                             f"object besides text; it is not rewritten here — edit "
                             f"it in Google Docs.", "kind": "invalid"}, "error"
        if len(paragraph.text) > MAX_KEY_TEXT:
            return {"error": f"Paragraph {number} is {len(paragraph.text):,} characters, "
                             f"more than an edit can record.", "kind": "invalid"}, "error"
        if paragraph.text == new_text:
            return {"error": f"Paragraph {number} already reads exactly that.",
                    "kind": "invalid"}, "error"
        title = str(document.get("title") or "")
        revision_id = str(document.get("revisionId") or "")
        record = await call.resources.create_data("edit", {
            "document_id": document_id, "title": title, "paragraph": number,
            "old_text": paragraph.text, "new_text": new_text,
            "revision_id": revision_id, "status": "proposed",
            "note": str(call.inputs.get("note") or "")})
        return {"edit_ref": record["resource_ref"], "title": title, "paragraph": number,
                "old_text": paragraph.text, "new_text": new_text,
                "revision_id": revision_id, "link": doc_link(document_id)}, "success"

    async def apply(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        edit_ref = str(call.inputs["edit_ref"])
        record = await call.resources.read_data("edit", edit_ref)
        keys = record.get("keys") or {}
        status = str(keys.get("status") or "")
        if status == "unknown":
            return {"error": "This edit's last apply had no answer from Google. Open "
                             "the document to see whether it landed; it is not sent "
                             "again.", "kind": "unknown"}, "error"
        if status != "proposed":
            return {"error": f"This edit is {status}; only a proposed edit can be "
                             f"applied. Read the document and propose afresh.",
                    "kind": "invalid"}, "error"
        document_id = str(keys.get("document_id") or "")
        number = int(keys.get("paragraph") or 0)
        old_text, new_text = str(keys.get("old_text") or ""), str(keys.get("new_text") or "")
        revision_id = str(keys.get("revision_id") or "")
        try:
            document = client.document(document_id)
        except GoogleError as exc:
            if exc.kind == "not_found":
                return await self._refuse(call, edit_ref, exc.message, "not_found")
            return failure(exc)
        current = str(document.get("revisionId") or "")
        if current != revision_id:
            return await self._refuse(
                call, edit_ref, "The document changed after this edit was proposed "
                                "(or the proposal is over a day old, past which Google "
                                "no longer honours its revision). Read it again and "
                                "propose afresh.")
        found = paragraphs(document)
        paragraph = found[number - 1] if 1 <= number <= len(found) else None
        if paragraph is None or paragraph.text != old_text or not paragraph.plain:
            return await self._refuse(call, edit_ref, f"Paragraph {number} no longer reads "
                                                      f"as it did when proposed.")
        requests_ = []
        if paragraph.text:
            # Up to, not including, the paragraph's own newline: the
            # paragraph and its style stay; only its words are replaced.
            requests_.append({"deleteContentRange": {"range": {
                "startIndex": paragraph.start, "endIndex": paragraph.end - 1}}})
        if new_text:
            requests_.append({"insertText": {"location": {"index": paragraph.start},
                                             "text": new_text}})
        await call.progress(f"Applying the edit to paragraph {number} of {keys.get('title')}")
        try:
            answer = client.batch_update(document_id, requests_, revision_id)
        except GoogleError as exc:
            if exc.kind == "unknown":
                await call.resources.update_data("edit", edit_ref, {
                    "status": "unknown", "reason": exc.message})
                return failure(exc)
            if exc.kind == "auth":
                return failure(exc)
            return await self._refuse(call, edit_ref, exc.message, exc.kind)
        applied_revision = str((answer.get("writeControl") or {}).get("requiredRevisionId") or "")
        await call.resources.update_data("edit", edit_ref, {
            "status": "applied", "applied_revision_id": applied_revision})
        return {"applied": True, "status": "applied", "paragraph": number,
                "revision_id": applied_revision, "link": doc_link(document_id)}, "success"

    @staticmethod
    async def _refuse(call, edit_ref, reason, kind="conflict"):
        await call.resources.update_data("edit", edit_ref, {"status": "refused", "reason": reason})
        return {"error": reason + " Nothing in the document was changed.",
                "kind": kind, "status": "refused"}, "error"

    async def discard(self, call):
        edit_ref = str(call.inputs["edit_ref"])
        record = await call.resources.read_data("edit", edit_ref)
        status = str((record.get("keys") or {}).get("status") or "")
        if status == "applied":
            return {"error": "This edit is already in the document; discarding the "
                             "record would not undo it.", "kind": "invalid"}, "error"
        await call.resources.update_data("edit", edit_ref, {"status": "discarded"})
        return {"discarded": True}, "success"
