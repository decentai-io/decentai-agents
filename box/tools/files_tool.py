"""Finding, fetching and filing in Box.

The same contract as the other drive agents' files tools. Box addresses
by id and keeps a path for every item, so a folder the user names is
walked to one segment at a time and a row says the path of the folder
it sits in. The top folder ("All Files") is id "0".
"""

import base64
import os
import time

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .box_api import ROOT, BoxError, item_row, kind_of, split_token

#: The platform's upload limit, and the file slots the manifest
#: declares. Bytes past the wire's line travel through the worker's
#: spool now, so the wire no longer caps a file; what the platform
#: will store does. Refused here, with its size, before the bytes
#: are asked for.
MAX_BYTES = 25 * 1024 * 1024
CONVERTIBLE = (".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods",
               ".ppt", ".pptx", ".odp")
#: Box makes a PDF on first request; it is asked after this many times,
#: two seconds apart, before the user is told to try again.
PDF_POLLS = 5
FREE_NAME_TRIES = 50


def too_large(name: str, size: int, fetched: bool = False):
    if fetched:
        text = (f"'{name}' came to {size // 1024:,} KB, over the {MAX_BYTES // 1024:,} "
                f"KB this platform can carry. It was not saved.")
    else:
        text = (f"'{name}' is {size // 1024:,} KB, and this platform can carry at "
                f"most {MAX_BYTES // 1024:,} KB. It was not downloaded. Nothing is "
                f"wrong with the file — it is too large to hand to another agent.")
    return {"error": text, "kind": "too_large", "size": size, "limit": MAX_BYTES}, "error"


def numbered(name: str, n: int) -> str:
    """ "notice.pdf" as "notice 1.pdf", the drive twins' spelling."""
    stem, ext = os.path.splitext(name)
    return f"{stem} {n}{ext}"


class FilesTool(ToolBase):
    id = "files"

    @staticmethod
    def _folder(client, folder_id, folder_path):
        """The folder the user named, as (id, None) — or ("", result)
        when there is no folder there to use."""
        if folder_id:
            try:
                return str(client.folder(str(folder_id)).get("id") or folder_id), None
            except BoxError as exc:
                if exc.kind == "not_found":
                    return "", ({"error": f"Box has no folder with id '{folder_id}'.",
                                 "kind": "not_found"}, "error")
                raise
        parts = [p for p in str(folder_path or "/").replace("\\", "/").split("/") if p.strip()]
        current = ROOT
        for index, part in enumerate(parts):
            found = FilesTool._child_folder(client, current, part)
            if not found:
                path = "/" + "/".join(parts[: index + 1])
                return "", ({"error": f"Box has no folder at '{path}'.",
                             "kind": "not_found"}, "error")
            current = found
        return current, None

    @staticmethod
    def _child_folder(client, parent_id, name):
        offset = 0
        while True:
            page = client.items(parent_id, 1000, offset)
            entries = page.get("entries") or []
            for entry in entries:
                if entry.get("type") == "folder" and \
                        str(entry.get("name") or "").lower() == name.lower():
                    return str(entry["id"])
            offset += len(entries)
            if not entries or offset >= int(page.get("total_count") or 0):
                return ""

    @staticmethod
    def _page(answer, rows, token_prefix=""):
        result = {"items": rows}
        offset = int(answer.get("offset") or 0) + len(answer.get("entries") or [])
        if answer.get("entries") and offset < int(answer.get("total_count") or 0):
            result["next_page_token"] = f"{token_prefix}{offset}"
        return result

    # -- reads -----------------------------------------------------------
    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        token = str(call.inputs.get("page_token") or "0")
        if not token.isdigit():
            return {"error": "That page_token is not one this agent gave."}, "error"
        try:
            answer = client.search(str(call.inputs["query"]),
                                   int(call.inputs.get("max_results") or 20), int(token))
        except BoxError as exc:
            return failure(exc)
        rows = [item_row(i) for i in answer.get("entries") or []]
        return self._page(answer, rows), "success"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        limit = int(call.inputs.get("max_results") or 25)
        token = str(call.inputs.get("page_token") or "")
        try:
            if token:
                folder_id, offset = split_token(token)
            else:
                folder_id, problem = self._folder(client, call.inputs.get("folder_id"),
                                                  call.inputs.get("folder_path"))
                if problem:
                    return problem
                offset = 0
            answer = client.items(folder_id, limit, offset)
        except ValueError:
            return {"error": "That page_token is not one this agent gave."}, "error"
        except BoxError as exc:
            return failure(exc)
        rows = [item_row(i) for i in answer.get("entries") or []
                if i.get("type") in ("file", "folder")]
        return self._page(answer, rows, f"{folder_id}:"), "success"

    async def shared_with_me(self, call):
        """Box has no "shared with me" list: an item someone else owns and
        shared with the account sits at its top level, so that is where
        it is looked for."""
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        limit = int(call.inputs.get("max_results") or 25)
        try:
            me = (client.email or str(client.me().get("login") or "")).lower()
            entries = client.items(ROOT, 1000).get("entries") or []
        except BoxError as exc:
            return failure(exc)
        theirs = [e for e in entries
                  if str((e.get("owned_by") or {}).get("login") or "").lower() not in ("", me)]
        theirs.sort(key=lambda e: str(e.get("modified_at") or ""), reverse=True)
        rows = []
        for entry in theirs[:limit]:
            row = item_row(entry)
            row["shared"] = True
            row["shared_by"] = str((entry.get("owned_by") or {}).get("login") or "")
            rows.append(row)
        return {"items": rows}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.item(str(call.inputs["item_id"]))
        except BoxError as exc:
            return failure(exc)
        return {"item": item_row(item)}, "success"

    async def download(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        try:
            item = client.item(item_id)
        except BoxError as exc:
            return failure(exc)
        name = str(item.get("name") or "")
        if kind_of(item) == "folder":
            return {"error": f"'{name}' is a folder; download the files in it "
                             f"one by one."}, "error"
        stem, ext = os.path.splitext(name)
        convert = bool(call.inputs.get("as_pdf")) and ext.lower() != ".pdf"
        if convert and ext.lower() not in CONVERTIBLE:
            return {"error": f"Only Word, Excel and PowerPoint files convert to PDF; "
                             f"download '{name}' as it is."}, "error"
        size = int(item.get("size") or 0)
        if size > MAX_BYTES:
            return too_large(name, size)
        filename = stem + ".pdf" if convert else name
        await call.progress(f"Downloading {filename}")
        try:
            if convert:
                raw = self._pdf(client, item_id)
                if raw is None:
                    return {"error": f"Box is still making a PDF of '{name}'; try again "
                                     f"in a minute."}, "error"
            else:
                raw = client.content(item_id)
        except BoxError as exc:
            return failure(exc)
        # A PDF rendering is a different file from the one measured above,
        # so what came back is measured too.
        if len(raw) > MAX_BYTES:
            return too_large(filename, len(raw), fetched=True)
        saved = await call.resources.create_file(
            "download", filename, content_base64=base64.b64encode(raw).decode("ascii"))
        return {"file_ref": saved["resource_ref"], "filename": filename,
                "size": len(raw)}, "success"

    @staticmethod
    def _pdf(client, file_id):
        """Box's PDF of a document: asked for, made if it was never made,
        and waited on briefly. None while Box is still making it."""
        for attempt in range(PDF_POLLS):
            representation = client.pdf_representation(file_id)
            if not representation:
                raise BoxError("http", "Box offers no PDF of this file.")
            state = str((representation.get("status") or {}).get("state") or "")
            if state == "success":
                template = str((representation.get("content") or {}).get("url_template") or "")
                return client.fetch(template.replace("{+asset_path}", ""))
            if state == "error":
                raise BoxError("http", "Box could not make a PDF of this file.")
            if state == "none" and attempt == 0:
                client.fetch_json(str((representation.get("info") or {}).get("url") or ""))
            time.sleep(2)
        return None

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
            folder_id, problem = self._folder(client, inputs.get("folder_id"),
                                              inputs.get("folder_path"))
            if problem:
                return problem
            chosen, clash = self._free_name(client, folder_id, name, len(raw), on_conflict)
            if clash and on_conflict == "fail":
                return {"error": f"The folder already has a file named '{name}'."}, "error"
            await call.progress(f"Uploading {chosen} ({len(raw)} bytes)")
            if clash and on_conflict == "replace":
                if clash.get("type") != "file":
                    return {"error": f"'{name}' in that folder is a folder, not a "
                                     f"file to replace."}, "error"
                item = client.upload_version(str(clash["id"]), name, raw)
            else:
                item = client.upload(folder_id, chosen, raw)
        except BoxError as exc:
            return failure(exc)
        return {"item": item_row(item)}, "success"

    @staticmethod
    def _free_name(client, folder_id, name, size, on_conflict):
        """The name to upload under, and what already holds the asked-for
        name. Box refuses a taken name rather than renaming, so a name is
        checked before the upload — a check writes nothing."""
        try:
            client.preflight(folder_id, name, size)
            return name, {}
        except BoxError as exc:
            if exc.code != "item_name_in_use":
                raise
            clash = exc.conflict or {"type": "file"}
        if on_conflict != "rename":
            return name, clash
        for n in range(1, FREE_NAME_TRIES + 1):
            try:
                client.preflight(folder_id, numbered(name, n), size)
                return numbered(name, n), {}
            except BoxError as exc:
                if exc.code != "item_name_in_use":
                    raise
        raise BoxError("http", f"The folder has too many files named like '{name}'; "
                               f"give the upload a name.")

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
            item = client.create_folder(parent_id, name)
        except BoxError as exc:
            return failure(exc)
        return {"item": item_row(item)}, "success"

    async def move(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        body = {}
        if inputs.get("new_name"):
            body["name"] = str(inputs["new_name"]).strip()
        try:
            item = client.item(str(inputs["item_id"]))
            if inputs.get("to_folder_id") or inputs.get("to_folder_path"):
                target, problem = self._folder(client, inputs.get("to_folder_id"),
                                               inputs.get("to_folder_path"))
                if problem:
                    return problem
                body["parent"] = {"id": target}
            if not body:
                return {"error": "Nothing to change: give new_name, or a folder "
                                 "to move to."}, "error"
            moved = client.update(kind_of(item), str(item["id"]), body)
        except BoxError as exc:
            return failure(exc)
        return {"item": item_row(moved)}, "success"

    async def delete(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.item(str(call.inputs["item_id"]))
            client.trash(kind_of(item), str(item["id"]))
        except BoxError as exc:
            return failure(exc)
        return {"deleted": True, "name": str(item.get("name") or ""),
                "note": "In the Box trash; it can be restored from there for as long "
                        "as the account keeps trash (30 days unless the company set "
                        "otherwise)."}, "success"
