"""Edits: proposed on the platform, saved into the Word file only on a
separate level-3 call.

A proposal records one paragraph's text as it was read, the text to
put in its place, and the file's eTag at that moment. Nothing in the
file changes. Applying checks the eTag is still the same (otherwise the
file changed and the edit is refused), downloads the file fresh,
replaces that paragraph's words, and uploads the whole file with
``If-Match`` on the recorded eTag — so a save by someone else between
that check and the upload is refused by OneDrive (412) rather than
overwritten.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, open_document
from .docx_file import NotEditable, WordPackage
from .graph_word import GraphError, person

#: The platform's upload limit, and the file slots the manifest
#: declares. Bytes past the wire's line travel through the worker's
#: spool now, so the wire no longer caps a file; what the platform
#: will store does. Refused here, with its size, before the bytes
#: are asked for.
MAX_BYTES = 25 * 1024 * 1024
MAX_KEY_TEXT = 8000   # a record key holds at most 8,192 characters


class EditsTool(ToolBase):
    id = "edits"

    async def propose(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        number = int(call.inputs["paragraph"])
        new_text = str(call.inputs["new_text"])
        if "\n" in new_text or "\r" in new_text:
            return {"error": "new_text is one paragraph and cannot contain a line "
                             "break; propose each paragraph separately.",
                    "kind": "invalid"}, "error"
        try:
            item, package = open_document(client, item_id)
        except GraphError as exc:
            return failure(exc)
        total = len(package.paragraphs)
        if not 1 <= number <= total:
            return {"error": f"The document has {total} paragraphs; there is no "
                             f"paragraph {number}.", "kind": "invalid"}, "error"
        old_text = package.paragraphs[number - 1].text
        if not package.plain(number):
            return {"error": f"Paragraph {number} holds tracked changes, a picture, an "
                             f"object or an equation; it is not rewritten here — edit "
                             f"it in Word.", "kind": "invalid"}, "error"
        if len(old_text) > MAX_KEY_TEXT:
            return {"error": f"Paragraph {number} is {len(old_text):,} characters, more "
                             f"than an edit can record.", "kind": "invalid"}, "error"
        if old_text == new_text:
            return {"error": f"Paragraph {number} already reads exactly that.",
                    "kind": "invalid"}, "error"
        name, etag = str(item.get("name") or ""), str(item.get("eTag") or "")
        record = await call.resources.create_data("edit", {
            "item_id": item_id, "name": name, "paragraph": number,
            "old_text": old_text, "new_text": new_text, "etag": etag,
            "status": "proposed", "note": str(call.inputs.get("note") or "")})
        return {"edit_ref": record["resource_ref"], "name": name, "paragraph": number,
                "old_text": old_text, "new_text": new_text, "etag": etag,
                "link": str(item.get("webUrl") or "")}, "success"

    async def apply(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        edit_ref = str(call.inputs["edit_ref"])
        record = await call.resources.read_data("edit", edit_ref)
        keys = record.get("keys") or {}
        status = str(keys.get("status") or "")
        if status == "unknown":
            return {"error": "This edit's last save had no answer from Microsoft. Open "
                             "the document to see whether it landed; it is not sent "
                             "again.", "kind": "unknown"}, "error"
        if status != "proposed":
            return {"error": f"This edit is {status}; only a proposed edit can be "
                             f"applied. Read the document and propose afresh.",
                    "kind": "invalid"}, "error"
        item_id = str(keys.get("item_id") or "")
        number = int(keys.get("paragraph") or 0)
        old_text, new_text = str(keys.get("old_text") or ""), str(keys.get("new_text") or "")
        etag = str(keys.get("etag") or "")
        try:
            item = client.item(item_id)
        except GraphError as exc:
            if exc.kind == "not_found":
                return await self._refuse(call, edit_ref, exc.message, "not_found")
            return failure(exc)
        if str(item.get("eTag") or "") != etag:
            who = person(item.get("lastModifiedBy")) or "someone"
            return await self._refuse(
                call, edit_ref, f"The file changed after this edit was proposed (saved by "
                                f"{who} at {item.get('lastModifiedDateTime')}). Read it again "
                                f"and propose afresh.")
        size = int(item.get("size") or 0)
        if size > MAX_BYTES:
            return await self._refuse(
                call, edit_ref, f"The document is {size // 1024:,} KB, and this platform "
                                f"edits files of at most {MAX_BYTES // 1024:,} KB.", "too_large")
        try:
            _, package = open_document(client, item_id)
        except GraphError as exc:
            return failure(exc)
        paragraph = package.paragraphs[number - 1] if 1 <= number <= len(package.paragraphs) else None
        if paragraph is None or paragraph.text != old_text:
            return await self._refuse(call, edit_ref, f"Paragraph {number} no longer reads "
                                                      f"as it did when proposed.")
        try:
            raw = package.replace_text(number, new_text)
        except NotEditable as exc:
            return await self._refuse(call, edit_ref, str(exc), "invalid")
        if len(raw) > MAX_BYTES:
            return await self._refuse(
                call, edit_ref, f"The edited document would be {len(raw) // 1024:,} KB, over "
                                f"the {MAX_BYTES // 1024:,} KB this platform edits.", "too_large")
        # The file must still open before it replaces the person's copy.
        WordPackage(raw)
        await call.progress(f"Saving the edit to paragraph {number} of {keys.get('name')}")
        try:
            saved = client.replace_content(item_id, raw, etag)
        except GraphError as exc:
            if exc.kind == "unknown":
                await call.resources.update_data("edit", edit_ref, {
                    "status": "unknown", "reason": exc.message})
                return failure(exc)
            if exc.kind == "auth":
                return failure(exc)
            return await self._refuse(call, edit_ref, exc.message, exc.kind)
        new_etag = str(saved.get("eTag") or "")
        await call.resources.update_data("edit", edit_ref, {
            "status": "applied", "applied_etag": new_etag})
        return {"applied": True, "status": "applied", "paragraph": number,
                "etag": new_etag, "link": str(saved.get("webUrl") or "")}, "success"

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
