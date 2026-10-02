"""Finding, fetching and filing in Dropbox.

The same contract as the Google Drive and OneDrive agents' files tools.
Dropbox has real paths, so a row says the folder it sits in as a path;
results carry Dropbox's own ids ("id:…"), and every function accepts
either an id or a path.
"""

import base64
import os

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .dropbox_api import DropboxError, clean_path, item_row, tag

#: The platform's upload limit, and the file slots the manifest
#: declares. Bytes past the wire's line travel through the worker's
#: spool now, so the wire no longer caps a file; what the platform
#: will store does. Refused here, with its size, before the bytes
#: are asked for.
MAX_BYTES = 25 * 1024 * 1024
#: What Dropbox's preview renders as PDF. Spreadsheets preview as HTML,
#: so they are not offered as PDF at all.
PDF_PREVIEWS = (".doc", ".docx", ".docm", ".odt", ".rtf", ".ppt", ".pptx",
                ".pptm", ".pps", ".ppsx", ".ppsm", ".odp", ".ai", ".eps")


def too_large(name: str, size: int, fetched: bool = False):
    if fetched:
        text = (f"'{name}' came to {size // 1024:,} KB, over the {MAX_BYTES // 1024:,} "
                f"KB this platform can carry. It was not saved.")
    else:
        text = (f"'{name}' is {size // 1024:,} KB, and this platform can carry at "
                f"most {MAX_BYTES // 1024:,} KB. It was not downloaded. Nothing is "
                f"wrong with the file — it is too large to hand to another agent.")
    return {"error": text, "kind": "too_large", "size": size, "limit": MAX_BYTES}, "error"


def join(folder: str, name: str) -> str:
    return clean_path(f"{folder}/{name}")


def conflict(name: str):
    """A Dropbox refusal that means the name is taken, said plainly."""
    return {"error": f"That folder already has something named '{name}'.",
            "kind": "http"}, "error"


def is_folder(item) -> bool:
    return tag(item) == "folder"


class FilesTool(ToolBase):
    id = "files"

    @staticmethod
    def _folder(client, folder_id, folder_path):
        """The folder the user named, as (path, None) — or ("", result)
        when there is no folder there to use. Writes need a path, so a
        folder given by id is looked up once."""
        if not folder_id and clean_path(folder_path or "/") == "/":
            return "/", None
        wanted = str(folder_id or clean_path(folder_path))
        try:
            folder = client.metadata(wanted)
        except DropboxError as exc:
            if exc.kind == "not_found":
                return "", ({"error": f"Dropbox has no folder at '{wanted}'.",
                             "kind": "not_found"}, "error")
            raise
        if not is_folder(folder):
            return "", ({"error": f"'{wanted}' is a file, not a folder."}, "error")
        return str(folder.get("path_display") or wanted), None

    @staticmethod
    def _item(client, item_id):
        """An item in the account's Dropbox, or a file someone shared that
        was never added to it — Dropbox answers for those only through
        its sharing calls. The second value says which it was."""
        try:
            return client.metadata(item_id), False
        except DropboxError as exc:
            if exc.kind != "not_found" or not str(item_id).startswith("id:"):
                raise
        return client.received_file(str(item_id)), True

    @staticmethod
    def _received_row(entry):
        return {"item_id": str(entry.get("id") or ""), "name": str(entry.get("name") or ""),
                "kind": "file", "size": 0, "mime_type": "", "modified": "",
                "modified_by": "", "folder": "", "path": str(entry.get("path_display") or ""),
                "link": str(entry.get("preview_url") or ""), "shared": True,
                "shared_by": ", ".join(entry.get("owner_display_names") or [])}

    # -- reads -----------------------------------------------------------
    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            answer = client.search(str(call.inputs["query"]),
                                   int(call.inputs.get("max_results") or 20),
                                   str(call.inputs.get("page_token") or ""))
            found = [((m.get("metadata") or {}).get("metadata") or {})
                     for m in answer.get("matches") or []]
            rows = [item_row(i, client) for i in found if i]
        except DropboxError as exc:
            return failure(exc)
        result = {"items": rows}
        if answer.get("has_more") and answer.get("cursor"):
            result["next_page_token"] = str(answer["cursor"])
        return result, "success"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        page_token = str(call.inputs.get("page_token") or "")
        try:
            if page_token:
                answer = client.list_folder_continue(page_token)
            else:
                if call.inputs.get("folder_id"):
                    where = str(call.inputs["folder_id"])
                    folder, problem = self._folder(client, where, "")
                else:
                    folder, problem = self._folder(client, "", call.inputs.get("folder_path"))
                    where = folder
                if problem:
                    return problem
                answer = client.list_folder(where, int(call.inputs.get("max_results") or 25))
            rows = [item_row(i, client) for i in answer.get("entries") or []
                    if tag(i) in ("file", "folder")]
        except DropboxError as exc:
            return failure(exc)
        # Dropbox lists in no promised order; folders first, then by name,
        # as the drive twins show a folder.
        rows.sort(key=lambda r: (r["kind"] != "folder", r["name"].lower()))
        result = {"items": rows}
        if answer.get("has_more") and answer.get("cursor"):
            result["next_page_token"] = str(answer["cursor"])
        return result, "success"

    async def shared_with_me(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        limit = int(call.inputs.get("max_results") or 25)
        try:
            entries = client.received_files(limit)
        except DropboxError as exc:
            return failure(exc)
        entries.sort(key=lambda e: str(e.get("time_invited") or ""), reverse=True)
        return {"items": [self._received_row(e) for e in entries][:limit]}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item, received = self._item(client, str(call.inputs["item_id"]))
            row = self._received_row(item) if received else item_row(item, client)
        except DropboxError as exc:
            return failure(exc)
        return {"item": row}, "success"

    async def download(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        as_pdf = bool(call.inputs.get("as_pdf"))
        try:
            item, received = self._item(client, item_id)
            if received:
                return await self._download_received(call, client, item, as_pdf)
        except DropboxError as exc:
            return failure(exc)
        name = str(item.get("name") or "")
        if is_folder(item):
            return {"error": f"'{name}' is a folder; download the files in it "
                             f"one by one."}, "error"
        stem, ext = os.path.splitext(name)
        exported = item.get("is_downloadable") is False
        convert = as_pdf and ext.lower() != ".pdf" and not exported
        if as_pdf and exported:
            return {"error": f"'{name}' is a Dropbox document that is exported in "
                             f"Dropbox's own format; download it without as_pdf."}, "error"
        if convert and ext.lower() not in PDF_PREVIEWS:
            return {"error": f"Dropbox renders only Word and PowerPoint files (and "
                             f"similar documents) as PDF; download '{name}' as it "
                             f"is."}, "error"
        size = int(item.get("size") or 0)
        if not exported and size > MAX_BYTES:
            return too_large(name, size)
        filename = stem + ".pdf" if convert else name
        await call.progress(f"Downloading {filename}")
        try:
            if exported:
                result, raw = client.export(item_id)
                filename = str((result.get("export_metadata") or {}).get("name") or name)
            elif convert:
                raw = client.preview(item_id)
            else:
                _, raw = client.download(item_id)
        except DropboxError as exc:
            return failure(exc)
        # An export or a PDF rendering is a different file from the one
        # measured above, so what came back is measured too.
        if len(raw) > MAX_BYTES:
            return too_large(filename, len(raw), fetched=True)
        return await self._save(call, filename, raw)

    async def _download_received(self, call, client, item, as_pdf):
        """A shared file outside the account's Dropbox is fetched through
        its link; the link's metadata is what carries its size."""
        name = str(item.get("name") or "")
        url = str(item.get("preview_url") or "")
        if as_pdf and not name.lower().endswith(".pdf"):
            return {"error": f"'{name}' was shared with the account, and Dropbox "
                             f"converts only files in it; download it as it is."}, "error"
        if not url:
            return {"error": f"Dropbox gave no way to fetch '{name}'."}, "error"
        size = int(client.link_metadata(url).get("size") or 0)
        if size > MAX_BYTES:
            return too_large(name, size)
        await call.progress(f"Downloading {name}")
        raw = client.link_file(url)
        if len(raw) > MAX_BYTES:
            return too_large(name, len(raw), fetched=True)
        return await self._save(call, name, raw)

    @staticmethod
    async def _save(call, filename, raw):
        saved = await call.resources.create_file(
            "download", filename, content_base64=base64.b64encode(raw).decode("ascii"))
        return {"file_ref": saved["resource_ref"], "filename": filename,
                "size": len(raw)}, "success"

    # -- writes ----------------------------------------------------------
    async def upload(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        file_ref = str(inputs["file_ref"])
        try:
            record = await call.resources.read_file("outgoing", file_ref)
        except Exception:
            return {"error": f"Unknown file_ref '{file_ref}'."}, "error"
        raw = base64.b64decode(record["content_base64"]) if record.get("content_base64") \
            else str(record.get("content") or "").encode("utf-8")
        name = str(inputs.get("name") or record.get("filename") or "").strip()
        if not name:
            return {"error": "The file has no name; give one."}, "error"
        # Reaching here at all means the bytes already crossed the wire,
        # so this cannot save an oversized upload — the read above would
        # have killed the worker first. It stands as the honest ceiling
        # for anything that gets this far.
        if len(raw) > MAX_BYTES:
            return {"error": f"The file is {len(raw) // 1024:,} KB, over the "
                             f"{MAX_BYTES // 1024:,} KB this platform can carry.",
                    "kind": "too_large", "size": len(raw),
                    "limit": MAX_BYTES}, "error"
        on_conflict = str(inputs.get("on_conflict") or "rename")
        try:
            folder, problem = self._folder(client, inputs.get("folder_id"),
                                           inputs.get("folder_path"))
            if problem:
                return problem
            await call.progress(f"Uploading {name} ({len(raw)} bytes)")
            item = client.upload(join(folder, name), raw, on_conflict)
            row = item_row({".tag": "file", **item}, client)
        except DropboxError as exc:
            if "conflict" in exc.summary:
                return conflict(name)
            return failure(exc)
        return {"item": row}, "success"

    async def create_folder(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        name = str(call.inputs["name"]).strip()
        try:
            parent, problem = self._folder(client, call.inputs.get("folder_id"),
                                           call.inputs.get("folder_path"))
            if problem:
                return problem
            item = client.create_folder(join(parent, name))
        except DropboxError as exc:
            if "conflict" in exc.summary:
                return conflict(name)
            return failure(exc)
        return {"item": item_row({".tag": "folder", **item})}, "success"

    async def move(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        new_name = str(inputs.get("new_name") or "").strip()
        try:
            item = client.metadata(str(inputs["item_id"]))
            current = str(item.get("path_display") or "")
            target = current.rsplit("/", 1)[0] or "/"
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                target, problem = self._folder(client, inputs.get("to_folder_id"),
                                               inputs.get("to_folder_path"))
                if problem:
                    return problem
            destination = join(target, new_name or str(item.get("name") or ""))
            if destination == current:
                return {"error": "Nothing to change: give new_name, or a folder "
                                 "to move to."}, "error"
            moved = client.move(current, destination)
            row = item_row(moved, client)
        except DropboxError as exc:
            if "conflict" in exc.summary:
                return conflict(new_name or destination.rsplit("/", 1)[-1])
            return failure(exc)
        return {"item": row}, "success"

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.delete(str(call.inputs["item_id"]))
        except DropboxError as exc:
            return failure(exc)
        return {"deleted": True, "name": str(item.get("name") or ""),
                "note": "In Dropbox's deleted files; it can be restored from there "
                        "for as long as the account's plan keeps deleted "
                        "files."}, "success"
