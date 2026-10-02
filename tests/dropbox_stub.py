"""A loopback Dropbox: the API v2 subset the Dropbox agent calls, over
real HTTP, holding a small fictional Dropbox for Sidra Office Supplies.

It behaves the way Dropbox does where the agent depends on it: every
call is a POST; an RPC call takes a JSON body (``null`` for none) and a
content call takes its arguments in the ``Dropbox-API-Arg`` header,
answering with ``Dropbox-API-Result``; an error is a 409 whose
``error_summary`` reads like a path; the top level is the empty path;
items carry ids of the form ``id:…``; an upload in "add" mode with
autorename gets a numbered name — unless the bytes are the same, which
is no conflict at all; a file shared with the account but never added
to it answers only to the sharing calls; and a cursor answers with what
changed after it, a page at a time, until Dropbox declares it stale
("reset"). One address stands in for both of Dropbox's hosts.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PREVIEWS = (".doc", ".docx", ".ppt", ".pptx")


class DropboxStub:
    ACCOUNT = "demo@sidra.example"
    ACCOUNT_ID = "dbid:AADdemo"
    PEOPLE = {"dbid:AADdana": "dana@sidra.example"}
    TEAM = {"id": "dbtid:AAsidra", "name": "Sidra Office Supplies"}

    def __init__(self, team: bool = True):
        self.team = dict(self.TEAM) if team else None
        self.items: Dict[str, Dict[str, Any]] = {}       # path_lower -> item
        self.file_members: Dict[str, List[Dict[str, str]]] = {}
        self.folder_members: Dict[str, List[Dict[str, str]]] = {}
        self.links: Dict[str, List[Dict[str, Any]]] = {}  # item id -> links
        self.received: List[Dict[str, Any]] = []
        self.deleted_files: List[str] = []
        self.log: List[tuple] = []                        # (seq, entry)
        self.seq = 0
        self.reset_below = 0
        self.uploads: List[str] = []
        self.downloads: List[str] = []                    # content calls made
        self.drop_upload = False
        self.rate_limited = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return f"id:a4ayc_{self._n:04d}"

    def _stamp(self) -> str:
        return f"2026-09-01T10:{self.seq % 60:02d}:00Z"

    def _record(self, entry: Dict[str, Any]) -> None:
        self.seq += 1
        self.log.append((self.seq, dict(entry)))

    def _shared_folder_of(self, path_lower: str) -> str:
        for folder_path, item in self.items.items():
            sfid = (item.get("sharing_info") or {}).get("shared_folder_id")
            if sfid and path_lower.startswith(folder_path + "/"):
                return sfid
        return ""

    def add_folder(self, path: str, by: str = "") -> Dict[str, Any]:
        current = ""
        item: Dict[str, Any] = {}
        for part in [p for p in path.split("/") if p]:
            current = f"{current}/{part}"
            if current.lower() not in self.items:
                item = {".tag": "folder", "id": self._id(), "name": part,
                        "path_display": current, "path_lower": current.lower()}
                parent_sf = self._shared_folder_of(current.lower())
                if parent_sf:
                    item["sharing_info"] = {"read_only": False,
                                            "parent_shared_folder_id": parent_sf}
                self.items[current.lower()] = item
                self._record(self.view(item))
            item = self.items[current.lower()]
        return item

    def add_file(self, folder: str, name: str, content: bytes, by: str = "",
                 **extra) -> str:
        parent = self.add_folder(folder)["path_display"] if folder.strip("/") else ""
        path = f"{parent}/{name}"
        existing = self.items.get(path.lower())
        item = existing or {".tag": "file", "id": self._id(), "name": name,
                            "path_display": path, "path_lower": path.lower()}
        item.update({"size": len(content), "_content": content,
                     "server_modified": self._stamp(), **extra})
        parent_sf = self._shared_folder_of(path.lower())
        if parent_sf:
            item["sharing_info"] = {"read_only": False, "parent_shared_folder_id": parent_sf,
                                    "modified_by": by or self.ACCOUNT_ID}
        self.items[path.lower()] = item
        self._record(self.view(item))
        return item["id"]

    def delete(self, path: str) -> None:
        """Someone deletes an item (and what is under it)."""
        lower = path.lower()
        for key in [k for k in self.items if k == lower or k.startswith(lower + "/")]:
            gone = self.items.pop(key)
            self.deleted_files.append(gone["path_display"])
            self._record({".tag": "deleted", "name": gone["name"],
                          "path_display": gone["path_display"], "path_lower": key})

    def share_folder_with(self, path: str, email: str) -> str:
        folder = self.items[path.lower()]
        sfid = self._make_shared(folder)
        self.folder_members[sfid].append({"email": email, "access": "editor"})
        return sfid

    def _make_shared(self, folder: Dict[str, Any]) -> str:
        sfid = (folder.get("sharing_info") or {}).get("shared_folder_id")
        if sfid:
            return sfid
        self._n += 1
        sfid = f"84528{self._n:04d}"
        folder["sharing_info"] = {"read_only": False, "shared_folder_id": sfid}
        self.folder_members[sfid] = []
        for key, item in self.items.items():
            if key.startswith(folder["path_lower"] + "/"):
                item.setdefault("sharing_info", {})["parent_shared_folder_id"] = sfid
        return sfid

    def share_with_me(self, name: str, content: bytes, by: str) -> str:
        file_id = self._id()
        self.received.append({
            "id": file_id, "name": name, "_content": content, "size": len(content),
            "preview_url": f"https://www.dropbox.com/scl/fi/{file_id[3:]}/{name}?dl=0",
            "owner_display_names": [by], "time_invited": "2026-08-30T09:00:00Z",
            "access_type": {".tag": "viewer"}, "policy": {}})
        return file_id

    def invalidate_cursors(self) -> None:
        """Dropbox declares every cursor taken so far stale."""
        self.seq += 1
        self.reset_below = self.seq

    def by_path(self, path: str) -> Optional[Dict[str, Any]]:
        return self.items.get(path.lower())

    def view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _resolve(self, path: str) -> Optional[Dict[str, Any]]:
        if str(path).startswith("id:"):
            return next((i for i in self.items.values() if i["id"] == path), None)
        return self.items.get(str(path).lower())

    def _children(self, folder_lower: str) -> List[Dict[str, Any]]:
        return [i for k, i in self.items.items()
                if k.rsplit("/", 1)[0] == folder_lower]

    def _members(self, owner_first: List[Dict[str, str]], inherited: List[Dict[str, str]]):
        users = [{"access_type": {".tag": "owner"}, "is_inherited": False,
                  "user": {"account_id": self.ACCOUNT_ID, "email": self.ACCOUNT,
                           "display_name": "Demo"}}]
        for member, is_inherited in [(m, False) for m in owner_first] + \
                                    [(m, True) for m in inherited]:
            users.append({"access_type": {".tag": member["access"]},
                          "is_inherited": is_inherited,
                          "user": {"account_id": "dbid:" + member["email"],
                                   "email": member["email"], "display_name": member["email"]}})
        return {"users": users, "groups": [], "invitees": []}

    def _link_view(self, item: Dict[str, Any], link: Dict[str, Any]) -> Dict[str, Any]:
        audience = link["audience"]
        return {".tag": item[".tag"], "url": link["url"], "name": item["name"],
                "id": item["id"], "path_lower": item["path_lower"],
                "link_permissions": {
                    "resolved_visibility": {".tag": "team_only" if audience == "team" else "public"},
                    "effective_audience": {".tag": audience},
                    "link_access_level": {".tag": link["access"]}}}

    # -- the server ------------------------------------------------------
    def start(self) -> "DropboxStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _bytes(self, raw: bytes, result: Dict[str, Any]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Dropbox-API-Result", json.dumps(result))
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, summary: str, error: Optional[Dict[str, Any]] = None) -> None:
                self._json(409, {"error_summary": summary,
                                 "error": error or {".tag": summary.split("/")[0]}})

            def _not_found(self) -> None:
                self._error("path/not_found/..", {".tag": "path", "path": {".tag": "not_found"}})

            def do_POST(self):  # noqa: N802
                if self.headers.get("Authorization") != "Bearer at-1":
                    return self._json(401, {"error_summary": "expired_access_token/..",
                                            "error": {".tag": "expired_access_token"}})
                if stub.rate_limited:
                    return self._json(429, {"error_summary": "too_many_requests/.."},
                                      {"Retry-After": "30"})
                method = self.path.split("?", 1)[0][len("/2/"):]
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Dropbox-API-Arg") is not None:
                    header = self.headers["Dropbox-API-Arg"]
                    assert header.isascii(), "Dropbox-API-Arg must be ASCII"
                    return self._content(method, json.loads(header), raw)
                assert self.headers.get("Content-Type") == "application/json", method
                return self._rpc(method, json.loads(raw or b"null"))

            # -- content endpoints -----------------------------------------
            def _content(self, method: str, arg: Dict[str, Any], raw: bytes):
                if method == "files/upload":
                    stub.uploads.append(arg["path"].rsplit("/", 1)[-1])
                    if stub.drop_upload:
                        # The file was received; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    path = arg["path"]
                    existing = stub.items.get(path.lower())
                    if existing and arg["mode"] == "add" and existing.get("_content") != raw:
                        if not arg.get("autorename"):
                            return self._error("path/conflict/file/..")
                        stem, dot, ext = path.rpartition(".")
                        n = 1
                        while stub.items.get(f"{stem} ({n}).{ext}".lower()):
                            n += 1
                        path = f"{stem} ({n}).{ext}"
                    folder, name = path.rsplit("/", 1)
                    stub.add_file(folder, name, raw)
                    item = stub.view(stub.items[path.lower()])
                    item.pop(".tag")
                    return self._json(200, item)
                stub.downloads.append(method)
                if method == "sharing/get_shared_link_file":
                    entry = next((r for r in stub.received if r["preview_url"] == arg["url"]), None)
                    if entry is None:
                        return self._error("shared_link_not_found/..")
                    return self._bytes(entry["_content"], {".tag": "file", "name": entry["name"],
                                                           "size": entry["size"]})
                item = stub._resolve(arg["path"])
                if item is None:
                    return self._not_found()
                if item[".tag"] == "folder":
                    return self._error("path/not_file/..")
                if method == "files/download":
                    if item.get("is_downloadable") is False:
                        return self._error("unsupported_file/..")
                    return self._bytes(item["_content"], stub.view(item))
                if method == "files/get_preview":
                    if not item["name"].lower().endswith(PREVIEWS):
                        return self._error("unsupported_extension/..")
                    return self._bytes(b"%PDF-1.7 preview of " + item["name"].encode(), stub.view(item))
                if method == "files/export":
                    exported = b"# " + item["name"].encode() + b"\n"
                    return self._bytes(exported, {"export_metadata": {
                        "name": item["name"] + ".md", "size": len(exported)},
                        "file_metadata": stub.view(item)})
                self._error("no_route/..")

            # -- RPC endpoints ---------------------------------------------
            def _rpc(self, method: str, arg: Any):
                handler = getattr(self, "rpc_" + method.replace("/", "__"), None)
                if handler is None:
                    return self._json(400, {"error_summary": f"no route {method}"})
                return handler(arg or {})

            def rpc_users__get_current_account(self, arg):
                account = {"account_id": stub.ACCOUNT_ID, "email": stub.ACCOUNT,
                           "name": {"display_name": "Demo"},
                           "account_type": {".tag": "business" if stub.team else "basic"}}
                if stub.team:
                    account["team"] = stub.team
                self._json(200, account)

            def rpc_users__get_space_usage(self, arg):
                used = sum(i.get("size", 0) for i in stub.items.values())
                self._json(200, {"used": used, "allocation": {
                    ".tag": "individual", "allocated": 2 * 1024 ** 4}})

            def rpc_users__get_account(self, arg):
                email = stub.PEOPLE.get(arg["account_id"])
                if email is None:
                    return self._error("no_account/..")
                self._json(200, {"account_id": arg["account_id"], "email": email,
                                 "name": {"display_name": email}})

            def rpc_files__get_metadata(self, arg):
                if arg["path"] == "":
                    return self._json(400, {"error_summary": "The root folder is unsupported."})
                item = stub._resolve(arg["path"])
                if item is None:
                    return self._not_found()
                self._json(200, stub.view(item))

            def rpc_files__list_folder(self, arg):
                if arg["path"] and stub._resolve(arg["path"]) is None:
                    return self._not_found()
                folder = "" if not arg["path"] else stub._resolve(arg["path"])["path_lower"]
                self._page({"k": "list", "folder": folder, "offset": 0,
                            "limit": int(arg.get("limit") or 2000)})

            def _page(self, cursor):
                rows = stub._children(cursor["folder"])
                start, size = cursor["offset"], cursor["limit"]
                page = rows[start:start + size]
                more = start + size < len(rows)
                nxt = {**cursor, "offset": start + size} if more else \
                    {"k": "changes", "folder": cursor["folder"], "seq": stub.seq,
                     "limit": size}
                self._json(200, {"entries": [stub.view(i) for i in page],
                                 "cursor": json.dumps(nxt), "has_more": more})

            def rpc_files__list_folder__get_latest_cursor(self, arg):
                folder = arg["path"]
                if folder:
                    item = stub._resolve(folder)
                    if item is None:
                        return self._not_found()
                    folder = item["path_lower"]
                assert arg.get("recursive") is True
                self._json(200, {"cursor": json.dumps({
                    "k": "changes", "folder": folder, "seq": stub.seq,
                    "limit": int(arg.get("limit") or 2000)})})

            def rpc_files__list_folder__continue(self, arg):
                cursor = json.loads(arg["cursor"])
                if cursor["k"] == "list":
                    return self._page(cursor)
                if cursor["seq"] < stub.reset_below:
                    return self._error("reset/..", {".tag": "reset"})
                folder = cursor["folder"]
                pending = [(seq, e) for seq, e in stub.log if seq > cursor["seq"] and (
                    not folder or e["path_lower"] == folder
                    or e["path_lower"].startswith(folder + "/"))]
                page = pending[:cursor["limit"]]
                more = len(pending) > len(page)
                seq = page[-1][0] if more else stub.seq
                self._json(200, {"entries": [e for _, e in page], "has_more": more,
                                 "cursor": json.dumps({**cursor, "seq": seq})})

            def rpc_files__search_v2(self, arg):
                needle = arg["query"].lower()
                found = [i for i in stub.items.values()
                         if needle in i["name"].lower()
                         or needle in i.get("_content", b"").decode("utf-8", "replace").lower()]
                self._search(found, 0, arg["options"]["max_results"], arg["query"])

            def _search(self, found, start, size, query):
                page = found[start:start + size]
                more = start + size < len(found)
                self._json(200, {"matches": [{"match_type": {".tag": "filename"},
                                              "metadata": {".tag": "metadata",
                                                           "metadata": stub.view(i)}}
                                             for i in page],
                                 "has_more": more,
                                 "cursor": json.dumps({"q": query, "offset": start + size,
                                                       "limit": size}) if more else None})

            def rpc_files__search__continue_v2(self, arg):
                cursor = json.loads(arg["cursor"])
                needle = cursor["q"].lower()
                found = [i for i in stub.items.values() if needle in i["name"].lower()]
                self._search(found, cursor["offset"], cursor["limit"], cursor["q"])

            def rpc_files__create_folder_v2(self, arg):
                if arg["path"].lower() in stub.items:
                    return self._error("path/conflict/folder/..")
                item = stub.view(stub.add_folder(arg["path"]))
                item.pop(".tag")
                self._json(200, {"metadata": item})

            def rpc_files__move_v2(self, arg):
                item = stub._resolve(arg["from_path"])
                if item is None:
                    return self._not_found()
                to = arg["to_path"]
                if to.lower() in stub.items:
                    return self._error("to/conflict/file/..")
                old = item["path_lower"]
                for key in [k for k in list(stub.items) if k == old or k.startswith(old + "/")]:
                    moved = stub.items.pop(key)
                    stub._record({".tag": "deleted", "name": moved["name"],
                                  "path_display": moved["path_display"], "path_lower": key})
                    moved["path_display"] = to + moved["path_display"][len(old):]
                    moved["path_lower"] = moved["path_display"].lower()
                    if key == old:
                        moved["name"] = to.rsplit("/", 1)[-1]
                    stub.items[moved["path_lower"]] = moved
                    stub._record(stub.view(moved))
                self._json(200, {"metadata": stub.view(item)})

            def rpc_files__delete_v2(self, arg):
                item = stub._resolve(arg["path"])
                if item is None:
                    return self._not_found()
                stub.delete(item["path_display"])
                self._json(200, {"metadata": stub.view(item)})

            def rpc_sharing__list_received_files(self, arg):
                self._json(200, {"entries": [stub.view(r) for r in stub.received][:arg["limit"]]})

            def rpc_sharing__get_file_metadata(self, arg):
                entry = next((r for r in stub.received if r["id"] == arg["file"]), None)
                if entry is None:
                    return self._error("user_error/invalid_file/..")
                view = stub.view(entry)
                view.pop("size")        # Dropbox's shared-file metadata has no size
                self._json(200, view)

            def rpc_sharing__get_shared_link_metadata(self, arg):
                entry = next((r for r in stub.received if r["preview_url"] == arg["url"]), None)
                if entry is None:
                    return self._error("shared_link_not_found/..")
                self._json(200, {".tag": "file", "url": entry["preview_url"], "id": entry["id"],
                                 "name": entry["name"], "size": entry["size"]})

            def rpc_sharing__list_file_members(self, arg):
                item = stub._resolve(arg["file"])
                if item is None:
                    return self._error("user_error/invalid_file/..")
                parent_sf = (item.get("sharing_info") or {}).get("parent_shared_folder_id")
                inherited = stub.folder_members.get(parent_sf, []) if parent_sf else []
                self._json(200, stub._members(stub.file_members.get(item["id"], []), inherited))

            def rpc_sharing__add_file_member(self, arg):
                item = stub._resolve(arg["file"])
                access = arg["access_level"][".tag"]
                answers = []
                for member in arg["members"]:
                    stub.file_members.setdefault(item["id"], []).append(
                        {"email": member["email"], "access": access})
                    answers.append({"member": member, "result": {".tag": "success"}})
                self._json(200, answers)

            def rpc_sharing__remove_file_member_2(self, arg):
                item = stub._resolve(arg["file"])
                members = stub.file_members.get(item["id"], [])
                kept = [m for m in members if m["email"] != arg["member"].get("email")]
                if len(kept) == len(members):
                    return self._error("member_error/no_explicit_access/..")
                stub.file_members[item["id"]] = kept
                self._json(200, {".tag": "success"})

            def rpc_sharing__share_folder(self, arg):
                folder = stub._resolve(arg["path"])
                sfid = stub._make_shared(folder)
                self._json(200, {".tag": "complete", "shared_folder_id": sfid,
                                 "name": folder["name"], "path_lower": folder["path_lower"]})

            def rpc_sharing__add_folder_member(self, arg):
                for entry in arg["members"]:
                    stub.folder_members[arg["shared_folder_id"]].append(
                        {"email": entry["member"]["email"],
                         "access": entry["access_level"][".tag"]})
                self._json(200, None)

            def rpc_sharing__list_folder_members(self, arg):
                self._json(200, stub._members(stub.folder_members.get(arg["shared_folder_id"], []), []))

            def rpc_sharing__remove_folder_member(self, arg):
                members = stub.folder_members.get(arg["shared_folder_id"], [])
                stub.folder_members[arg["shared_folder_id"]] = [
                    m for m in members if m["email"] != arg["member"].get("email")]
                self._json(200, {".tag": "async_job_id", "async_job_id": "job-1"})

            def rpc_sharing__list_shared_links(self, arg):
                item = stub._resolve(arg["path"])
                if item is None:
                    return self._not_found()
                self._json(200, {"links": [stub._link_view(item, link)
                                           for link in stub.links.get(item["id"], [])],
                                 "has_more": False})

            def rpc_sharing__create_shared_link_with_settings(self, arg):
                item = stub._resolve(arg["path"])
                if item is None:
                    return self._not_found()
                existing = stub.links.get(item["id"])
                if existing:
                    return self._error("shared_link_already_exists/metadata/..", {
                        ".tag": "shared_link_already_exists",
                        "shared_link_already_exists": {
                            ".tag": "metadata", "metadata": stub._link_view(item, existing[0])}})
                audience = arg["settings"]["audience"][".tag"]
                if audience == "team" and not stub.team:
                    return self._error("settings_error/not_authorized/..")
                link = {"url": f"https://www.dropbox.com/scl/fi/{item['id'][3:]}/"
                               f"{item['name']}?rlkey=k{len(stub.links)}&dl=0",
                        "audience": audience, "access": arg["settings"]["access"][".tag"]}
                stub.links[item["id"]] = [link]
                self._json(200, stub._link_view(item, link))

            def rpc_sharing__modify_shared_link_settings(self, arg):
                for item_id, links in stub.links.items():
                    for link in links:
                        if link["url"] == arg["url"]:
                            link["audience"] = arg["settings"]["audience"][".tag"]
                            link["access"] = arg["settings"]["access"][".tag"]
                            item = next(i for i in stub.items.values() if i["id"] == item_id)
                            return self._json(200, stub._link_view(item, link))
                self._error("shared_link_not_found/..")

            def rpc_sharing__revoke_shared_link(self, arg):
                for item_id, links in stub.links.items():
                    if any(link["url"] == arg["url"] for link in links):
                        stub.links[item_id] = [l for l in links if l["url"] != arg["url"]]
                        return self._json(200, None)
                self._error("shared_link_not_found/..")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, access_token: str = "at-1") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
