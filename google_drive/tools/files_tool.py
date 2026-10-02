"""Finding, fetching and filing in Google Drive.

The same contract as the OneDrive agent's files tool. Drive has no
paths, so a folder the user names is walked to one segment at a time,
and each row says the name of the folder it sits in.
"""

import base64
import mimetypes
import os

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .drive_api import EXPORTS, FOLDER, GoogleError, item_row, literal

#: The platform's upload limit, and the file slots the manifest
#: declares. Bytes past the wire's line travel through the worker's
#: spool now, so the wire no longer caps a file; what the platform
#: will store does. Refused here, with its size, before the bytes
#: are asked for.
MAX_BYTES = 25 * 1024 * 1024
PDF = "application/pdf"


class Folders:
    """Parent ids to folder names, one lookup each, for a page of rows."""

    def __init__(self, client):
        self.client = client
        self.names = {}

    def name(self, item) -> str:
        parents = item.get("parents") or []
        if not parents:
            return ""
        parent = parents[0]
        if parent not in self.names:
            try:
                self.names[parent] = str(self.client.item(parent).get("name") or "")
            except GoogleError:
                self.names[parent] = ""
        return self.names[parent]

    def rows(self, items):
        return [item_row(i, self.name(i)) for i in items]


def _page(answer, rows):
    result = {"items": rows}
    if answer.get("nextPageToken"):
        result["next_page_token"] = str(answer["nextPageToken"])
    return result


def free_name(taken, name: str) -> str:
    """ "notice.pdf" as "notice 1.pdf", "notice 2.pdf", … — the first
    not already in the folder."""
    taken = {t.lower() for t in taken}
    if name.lower() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 1
    while f"{stem} {n}{ext}".lower() in taken:
        n += 1
    return f"{stem} {n}{ext}"


class FilesTool(ToolBase):
    id = "files"

    @staticmethod
    def _folder(client, folder_id, folder_path):
        """The folder the user named, as (id, None) — or ("", result)
        when there is no folder there to use."""
        if folder_id:
            return str(folder_id), None
        parts = [p for p in str(folder_path or "/").replace("\\", "/").split("/") if p.strip()]
        current = "root"
        for index, part in enumerate(parts):
            found = client.in_folder(current, part, folders_only=True)
            if not found:
                path = "/" + "/".join(parts[: index + 1])
                return "", ({"error": f"Google Drive has no folder at '{path}'.",
                             "kind": "not_found"}, "error")
            current = str(found[0]["id"])
        return current, None

    # -- reads -----------------------------------------------------------
    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        text = literal(str(call.inputs["query"]))
        try:
            answer = client.files(f"(name contains {text} or fullText contains {text}) "
                                  f"and trashed = false",
                                  int(call.inputs.get("max_results") or 20),
                                  str(call.inputs.get("page_token") or ""))
            rows = Folders(client).rows(answer.get("files") or [])
        except GoogleError as exc:
            return failure(exc)
        return _page(answer, rows), "success"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            folder_id, problem = self._folder(client, call.inputs.get("folder_id"),
                                              call.inputs.get("folder_path"))
            if problem:
                return problem
            answer = client.files(f"{literal(folder_id)} in parents and trashed = false",
                                  int(call.inputs.get("max_results") or 50),
                                  str(call.inputs.get("page_token") or ""),
                                  order_by="folder,name")
            rows = Folders(client).rows(answer.get("files") or [])
        except GoogleError as exc:
            return failure(exc)
        return _page(answer, rows), "success"

    async def shared_with_me(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            answer = client.files("sharedWithMe = true and trashed = false",
                                  int(call.inputs.get("max_results") or 25),
                                  order_by="sharedWithMeTime desc")
        except GoogleError as exc:
            return failure(exc)
        return {"items": [item_row(i) for i in answer.get("files") or []]}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.item(str(call.inputs["item_id"]))
            row = Folders(client).rows([item])[0]
        except GoogleError as exc:
            return failure(exc)
        return {"item": row}, "success"

    async def download(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        as_pdf = bool(call.inputs.get("as_pdf"))
        try:
            item = client.item(item_id)
        except GoogleError as exc:
            return failure(exc)
        name, mime = str(item.get("name") or ""), str(item.get("mimeType") or "")
        if mime == FOLDER:
            return {"error": f"'{name}' is a folder; download the files in it "
                             f"one by one."}, "error"
        native = mime.startswith("application/vnd.google-apps.")
        if native and mime not in EXPORTS:
            return {"error": f"'{name}' is a Google file of a kind that cannot be "
                             f"exported here (only Docs, Sheets and Slides)."}, "error"
        if not native and as_pdf and mime != PDF:
            return {"error": "Google Drive converts only Google Docs, Sheets and "
                             f"Slides to PDF; download '{name}' as it is."}, "error"
        size = int(item.get("size") or 0)
        if not native and size > MAX_BYTES:
            return {"error": f"'{name}' is {size // 1024:,} KB, and this platform "
                             f"can carry at most {MAX_BYTES // 1024:,} KB. It was "
                             f"not downloaded. Nothing is wrong with the file — it "
                             f"is too large to hand to another agent.",
                    "kind": "too_large", "size": size,
                    "limit": MAX_BYTES}, "error"
        if native:
            export_type, ext = (PDF, ".pdf") if as_pdf else EXPORTS[mime]
            filename = name + ext
        else:
            filename = name
        await call.progress(f"Downloading {filename}")
        try:
            raw = client.export(item_id, export_type) if native else client.media(item_id)
        except GoogleError as exc:
            return failure(exc)
        # A Google file has no size until it is exported, so this is the
        # only measurement that exists for one.
        if len(raw) > MAX_BYTES:
            return {"error": f"'{filename}' came to {len(raw) // 1024:,} KB, over "
                             f"the {MAX_BYTES // 1024:,} KB this platform can "
                             f"carry. It was not saved.",
                    "kind": "too_large", "size": len(raw),
                    "limit": MAX_BYTES}, "error"
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
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        on_conflict = str(inputs.get("on_conflict") or "rename")
        try:
            folder_id, problem = self._folder(client, inputs.get("folder_id"),
                                              inputs.get("folder_path"))
            if problem:
                return problem
            siblings = [f for f in client.in_folder(folder_id) if f.get("mimeType") != FOLDER]
            same = [f for f in siblings if str(f.get("name") or "").lower() == name.lower()]
            await call.progress(f"Uploading {name} ({len(raw)} bytes)")
            if same and on_conflict == "fail":
                return {"error": f"The folder already has a file named '{name}'."}, "error"
            if same and on_conflict == "replace":
                item = client.replace(str(same[0]["id"]), raw, mime)
            else:
                item = client.upload(folder_id, free_name([f.get("name") or "" for f in siblings], name),
                                     raw, mime)
            row = Folders(client).rows([item])[0]
        except GoogleError as exc:
            return failure(exc)
        return {"item": row}, "success"

    async def create_folder(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        name = str(call.inputs["name"]).strip()
        try:
            parent_id, problem = self._folder(client, call.inputs.get("folder_id"),
                                              call.inputs.get("folder_path"))
            if problem:
                return problem
            if client.in_folder(parent_id, name, folders_only=True):
                return {"error": f"That folder already has a folder named '{name}'."}, "error"
            item = client.create_folder(parent_id, name)
            row = Folders(client).rows([item])[0]
        except GoogleError as exc:
            return failure(exc)
        return {"item": row}, "success"

    async def move(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        item_id = str(inputs["item_id"])
        body, add, remove = {}, "", ""
        if inputs.get("new_name"):
            body["name"] = str(inputs["new_name"]).strip()
        try:
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                target, problem = self._folder(client, inputs.get("to_folder_id"),
                                               inputs.get("to_folder_path"))
                if problem:
                    return problem
                add = target
                remove = ",".join(client.item(item_id).get("parents") or [])
            if not body and not add:
                return {"error": "Nothing to change: give new_name, or a folder "
                                 "to move to."}, "error"
            item = client.update(item_id, body, add_parents=add, remove_parents=remove)
            row = Folders(client).rows([item])[0]
        except GoogleError as exc:
            return failure(exc)
        return {"item": row}, "success"

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.trash(str(call.inputs["item_id"]))
        except GoogleError as exc:
            return failure(exc)
        return {"deleted": True, "name": str(item.get("name") or ""),
                "note": "In the Google Drive trash; it can be restored from there "
                        "for 30 days."}, "success"
