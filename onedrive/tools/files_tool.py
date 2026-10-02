"""Finding, fetching and filing in OneDrive.

Reads go straight to Graph. A download becomes a platform file another
agent can read; an upload takes one from the platform. Everything that
changes the drive is level 3 in the manifest, so by the time it runs
here it has already waited for the user.
"""

import base64
import os

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph_drive import GraphError, clean_path, item_row

#: The platform's upload limit, and the file slots the manifest
#: declares. Bytes past the wire's line travel through the worker's
#: spool now, so the wire no longer caps a file; what the platform
#: will store does. Refused here, with its size, before the bytes
#: are asked for.
MAX_BYTES = 25 * 1024 * 1024
CONVERTIBLE = (".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods",
               ".ppt", ".pptx", ".odp")


def _page(answer, rows):
    result = {"items": rows}
    if answer.get("@odata.nextLink"):
        result["next_page_token"] = str(answer["@odata.nextLink"])
    return result


class FilesTool(ToolBase):
    id = "files"

    @staticmethod
    def _folder(client, folder_id, folder_path):
        """The folder the user named, as (id, None) — or ("", result)
        when there is no folder there to use."""
        if folder_id:
            return str(folder_id), None
        path = clean_path(folder_path or "/")
        try:
            folder = client.item_by_path(path)
        except GraphError as exc:
            if exc.kind == "not_found":
                return "", ({"error": f"OneDrive has no folder at '{path}'.",
                             "kind": "not_found"}, "error")
            raise
        if "folder" not in folder:
            return "", ({"error": f"'{path}' is a file, not a folder."}, "error")
        return str(folder.get("id") or ""), None

    # -- reads -----------------------------------------------------------
    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            answer = client.search(str(call.inputs["query"]),
                                   int(call.inputs.get("max_results") or 20),
                                   str(call.inputs.get("page_token") or ""))
        except GraphError as exc:
            return failure(exc)
        return _page(answer, [item_row(i) for i in answer.get("value") or []]), "success"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        page_token = str(call.inputs.get("page_token") or "")
        try:
            if page_token:
                answer = client.children("", 0, page_token)
            else:
                folder_id, problem = self._folder(client, call.inputs.get("folder_id"),
                                                  call.inputs.get("folder_path"))
                if problem:
                    return problem
                answer = client.children(folder_id, int(call.inputs.get("max_results") or 50))
        except GraphError as exc:
            return failure(exc)
        return _page(answer, [item_row(i) for i in answer.get("value") or []]), "success"

    async def shared_with_me(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            entries = client.shared_with_me()
        except GraphError as exc:
            return failure(exc)
        limit = int(call.inputs.get("max_results") or 25)
        return {"items": [item_row(e) for e in entries][:limit]}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        drive_id = str(call.inputs.get("drive_id") or "")
        try:
            item = client.item(str(call.inputs["item_id"]), drive_id)
        except GraphError as exc:
            return failure(exc)
        row = item_row(item)
        if drive_id:
            row["drive_id"] = drive_id
        return {"item": row}, "success"

    async def download(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        drive_id = str(call.inputs.get("drive_id") or "")
        try:
            row = item_row(client.item(item_id, drive_id))
        except GraphError as exc:
            return failure(exc)
        if row["kind"] == "folder":
            return {"error": f"'{row['name']}' is a folder; download the files in "
                             f"it one by one."}, "error"
        if row["size"] > MAX_BYTES:
            return {"error": f"'{row['name']}' is {row['size'] // 1024:,} KB, and "
                             f"this platform can carry at most "
                             f"{MAX_BYTES // 1024:,} KB. It was not downloaded. "
                             f"Nothing is wrong with the file — it is too large to "
                             f"hand to another agent.",
                    "kind": "too_large", "size": row["size"],
                    "limit": MAX_BYTES}, "error"
        stem, ext = os.path.splitext(row["name"])
        convert = bool(call.inputs.get("as_pdf")) and ext.lower() != ".pdf"
        if convert and ext.lower() not in CONVERTIBLE:
            return {"error": f"Only Word, Excel and PowerPoint files convert to PDF; "
                             f"download '{row['name']}' as it is."}, "error"
        filename = stem + ".pdf" if convert else row["name"]
        await call.progress(f"Downloading {row['name']}" + (" as PDF" if convert else ""))
        try:
            raw = client.content(item_id, drive_id, as_pdf=convert)
        except GraphError as exc:
            return failure(exc)
        if not raw and row["size"]:
            return {"error": "OneDrive returned no content for that file."}, "error"
        # Converting to PDF makes a different file from the one that was
        # measured above, so what came back is measured too.
        if len(raw) > MAX_BYTES:
            return {"error": f"'{filename}' came back {len(raw) // 1024:,} KB, over "
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
        file_ref = str(call.inputs["file_ref"])
        try:
            record = await call.resources.read_file("outgoing", file_ref)
        except Exception:
            return {"error": f"Unknown file_ref '{file_ref}'."}, "error"
        raw = base64.b64decode(record["content_base64"]) if record.get("content_base64") \
            else str(record.get("content") or "").encode("utf-8")
        name = str(call.inputs.get("name") or record.get("filename") or "").strip()
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
        try:
            folder_id, problem = self._folder(client, call.inputs.get("folder_id"),
                                              call.inputs.get("folder_path"))
            if problem:
                return problem
            await call.progress(f"Uploading {name} ({len(raw)} bytes)")
            item = client.upload(folder_id, name, raw,
                                 str(call.inputs.get("on_conflict") or "rename"))
        except GraphError as exc:
            return failure(exc)
        return {"item": item_row(item)}, "success"

    async def create_folder(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            parent_id, problem = self._folder(client, call.inputs.get("folder_id"),
                                              call.inputs.get("folder_path"))
            if problem:
                return problem
            item = client.create_folder(parent_id, str(call.inputs["name"]).strip())
        except GraphError as exc:
            return failure(exc)
        return {"item": item_row(item)}, "success"

    async def move(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        patch = {}
        if inputs.get("new_name"):
            patch["name"] = str(inputs["new_name"]).strip()
        try:
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                target, problem = self._folder(client, inputs.get("to_folder_id"),
                                               inputs.get("to_folder_path"))
                if problem:
                    return problem
                patch["parentReference"] = {"id": target}
            if not patch:
                return {"error": "Nothing to change: give new_name, or a folder "
                                 "to move to."}, "error"
            item = client.update(str(inputs["item_id"]), patch)
        except GraphError as exc:
            return failure(exc)
        return {"item": item_row(item)}, "success"

    async def move_many(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        if not (inputs.get("to_folder_id") or inputs.get("to_folder_path")):
            return {"error": "Give the folder to move to."}, "error"
        try:
            target, problem = self._folder(client, inputs.get("to_folder_id"),
                                           inputs.get("to_folder_path"))
        except GraphError as exc:
            return failure(exc)
        if problem:
            return problem
        moved, failed = [], []
        for item_id in dict.fromkeys(str(i) for i in inputs["item_ids"]):
            try:
                item = client.update(item_id, {"parentReference": {"id": target}})
                moved.append(item_row(item))
            except GraphError as exc:
                failed.append({"item_id": item_id, "error": exc.message, "kind": exc.kind})
        folder = moved[0]["folder"] if moved else clean_path(inputs.get("to_folder_path") or "")
        return ({"folder": folder, "moved": moved, "failed": failed},
                "success" if moved else "error")

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        try:
            name = str(client.item(item_id).get("name") or "")
            client.delete(item_id)
        except GraphError as exc:
            return failure(exc)
        return {"deleted": True, "name": name,
                "note": "In the OneDrive recycle bin; it can be restored from there."}, "success"
