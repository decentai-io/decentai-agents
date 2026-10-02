"""A loopback Box: the API 2.0 subset the Box agent calls, over real
HTTP, holding a small fictional Box for Sidra Office Supplies.

It behaves the way Box does where the agent depends on it: files and
folders live at separate addresses and the top folder is "0"; every
item carries its path_collection; a folder's items come folders first;
a taken name is refused with a 409 that names the item already there,
never renamed; an upload is multipart with the attributes part first;
a download is a 302 to another address; trash is not deletion; people
get access through collaborations, which reach down into sub-folders;
and the change stream is read by position, a chunk at a time, and may
deliver the same event twice. One address stands in for both of Box's
hosts.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

ROOT = "0"
POSITION_BASE = 1152922976252290000     # Box positions are large integers


class BoxStub:
    ACCOUNT = "demo@sidra.example"
    ENTERPRISE = {"id": "8801", "type": "enterprise", "name": "Sidra Office Supplies"}

    def __init__(self, enterprise: bool = True):
        self.enterprise = dict(self.ENTERPRISE) if enterprise else None
        self.items: Dict[str, Dict[str, Any]] = {
            ROOT: {"type": "folder", "id": ROOT, "name": "All Files", "_parent": None,
                   "owned_by": {"login": self.ACCOUNT}}}
        self.collaborations: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.uploads: List[str] = []
        self.downloads: List[str] = []       # ids whose bytes were fetched
        self.drop_upload = False
        self.rate_limited = False
        self.pdf_state = "success"
        self._n = 1000
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return str(self._n)

    def _stamp(self) -> str:
        return f"2026-09-01T10:{len(self.events) % 60:02d}:00-07:00"

    def _path(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        chain, parent = [], item.get("_parent")
        while parent is not None:
            folder = self.items[parent]
            chain.insert(0, {"type": "folder", "id": folder["id"], "name": folder["name"]})
            parent = folder.get("_parent")
        return chain

    def view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        chain = self._path(item)
        out["path_collection"] = {"total_count": len(chain), "entries": chain}
        if item.get("_parent") is not None:
            parent = self.items[item["_parent"]]
            out["parent"] = {"type": "folder", "id": parent["id"], "name": parent["name"]}
        out.setdefault("shared_link", None)
        return out

    def _emit(self, event_type: str, item: Dict[str, Any], by: str = "") -> Dict[str, Any]:
        event = {"type": "event", "event_id": f"evt-{len(self.events) + 1:04d}-"
                                              f"f7c1-4c2e-9d0b", "event_type": event_type,
                 "created_at": self._stamp(),
                 "created_by": {"type": "user", "login": by or self.ACCOUNT},
                 "source": self.view(item)}
        self.events.append(event)
        return event

    def redeliver(self, event: Dict[str, Any]) -> None:
        """Box sends an event it already sent."""
        self.events.append(json.loads(json.dumps(event)))

    def _child(self, parent_id: str, name: str) -> Optional[Dict[str, Any]]:
        return next((i for i in self.items.values() if i.get("_parent") == parent_id
                     and i["name"].lower() == name.lower()
                     and i.get("item_status") != "trashed"), None)

    def add_folder(self, path: str, owner: str = "") -> str:
        parent = ROOT
        for part in [p for p in path.split("/") if p]:
            found = self._child(parent, part)
            if found is None:
                found = {"type": "folder", "id": self._id(), "name": part, "_parent": parent,
                         "size": 0, "item_status": "active", "modified_at": self._stamp(),
                         "owned_by": {"login": owner or self.ACCOUNT},
                         "modified_by": {"login": owner or self.ACCOUNT}}
                self.items[found["id"]] = found
            parent = found["id"]
        return parent

    def add_file(self, folder: str, name: str, content: bytes, by: str = "",
                 owner: str = "") -> str:
        parent = self.add_folder(folder)
        item = {"type": "file", "id": self._id(), "name": name, "_parent": parent,
                "size": len(content), "_content": content, "item_status": "active",
                "modified_at": self._stamp(), "owned_by": {"login": owner or self.ACCOUNT},
                "modified_by": {"login": by or self.ACCOUNT}}
        self.items[item["id"]] = item
        self._emit("ITEM_UPLOAD", item, by)
        return item["id"]

    def trash(self, item_id: str, by: str = "") -> None:
        item = self.items[item_id]
        item["item_status"] = "trashed"
        self._emit("ITEM_TRASH", item, by)

    def share_with_me(self, name: str, content: bytes, owner: str) -> str:
        """Someone else's file, shared with the account: Box keeps it at
        the account's top level."""
        return self.add_file("/", name, content, by=owner, owner=owner)

    def collaborate(self, item_id: str, login: str, role: str = "editor") -> str:
        collab_id = self._id()
        item = self.items[item_id]
        self.collaborations[collab_id] = {
            "type": "collaboration", "id": collab_id, "role": role, "status": "accepted",
            "accessible_by": {"type": "user", "login": login, "name": login},
            "item": {"type": item["type"], "id": item_id, "name": item["name"]}}
        return collab_id

    def by_path(self, path: str) -> Optional[Dict[str, Any]]:
        item: Optional[Dict[str, Any]] = self.items[ROOT]
        for part in [p for p in path.split("/") if p]:
            item = self._child(item["id"], part)
            if item is None:
                return None
        return item

    def _live(self, item: Optional[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
        if item is None or item["type"] != kind or item.get("item_status") == "trashed":
            return None
        return item

    def _ancestors(self, item: Dict[str, Any]) -> List[str]:
        chain, current = [item["id"]], item.get("_parent")
        while current is not None:
            chain.append(current)
            current = self.items[current].get("_parent")
        return chain

    # -- the server ------------------------------------------------------
    def start(self) -> "BoxStub":
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

            def _empty(self, status: int = 204, headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _bytes(self, raw: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, status: int, code: str, message: str, conflicts: Any = None) -> None:
                body = {"type": "error", "status": status, "code": code, "message": message}
                if conflicts is not None:
                    body["context_info"] = {"conflicts": conflicts}
                self._json(status, body)

            def _not_found(self) -> None:
                self._error(404, "not_found", "Not Found")

            def _conflict(self, existing: Dict[str, Any], as_list: bool) -> None:
                summary = {"type": existing["type"], "id": existing["id"], "name": existing["name"]}
                self._error(409, "item_name_in_use", "Item with the same name already exists",
                            [summary] if as_list else summary)

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                return urllib.parse.unquote(url.path), query

            def _authed(self, path: str) -> bool:
                if path.startswith(("/dl/", "/reps/")):
                    return True           # pre-signed addresses
                if stub.rate_limited:
                    self._json(429, {"type": "error", "code": "rate_limit_exceeded"},
                               {"Retry-After": "30"})
                    return False
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._error(401, "unauthorized", "Unauthorized")
                    return False
                return True

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            # -- GET -----------------------------------------------------
            def do_GET(self):  # noqa: N802
                path, query = self._route()
                if not self._authed(path):
                    return
                if path == "/users/me":
                    used = sum(i.get("size", 0) for i in stub.items.values()
                               if i["type"] == "file" and i["owned_by"]["login"] == stub.ACCOUNT)
                    return self._json(200, {"type": "user", "login": stub.ACCOUNT, "name": "Demo",
                                            "space_amount": 10 * 1024 ** 3, "space_used": used,
                                            "enterprise": stub.enterprise})
                if path == "/search":
                    assert query.get("type") == "file"
                    needle = query["query"].lower()
                    found = [i for i in stub.items.values() if i["type"] == "file"
                             and i.get("item_status") != "trashed"
                             and (needle in i["name"].lower() or needle in
                                  i.get("_content", b"").decode("utf-8", "replace").lower())]
                    return self._slice(found, query)
                if path == "/events":
                    return self._events(query)
                if path.startswith("/dl/"):
                    item = stub.items[path[len("/dl/"):]]
                    stub.downloads.append(item["id"])
                    return self._bytes(item["_content"])
                if path.startswith("/reps/"):
                    item = stub.items[path[len("/reps/"):].strip("/")]
                    stub.downloads.append(item["id"])
                    return self._bytes(b"%PDF-1.7 box rendering of " + item["name"].encode())
                parts = path.strip("/").split("/")
                if len(parts) >= 2 and parts[0] in ("files", "folders"):
                    kind = parts[0][:-1]
                    item = stub._live(stub.items.get(parts[1]), kind)
                    if item is None:
                        return self._not_found()
                    tail = parts[2] if len(parts) > 2 else ""
                    if tail == "items":
                        rows = [i for i in stub.items.values() if i.get("_parent") == item["id"]
                                and i.get("item_status") != "trashed"]
                        assert query.get("sort") == "name"
                        rows.sort(key=lambda i: (i["type"] != "folder", i["name"].lower()))
                        return self._slice(rows, query)
                    if tail == "collaborations":
                        chain = stub._ancestors(item)
                        entries = [c for c in stub.collaborations.values()
                                   if c["item"]["id"] in chain]
                        return self._json(200, {"total_count": len(entries), "entries": entries})
                    if tail == "content":
                        return self._empty(302, {"Location": f"{stub.url}/dl/{item['id']}"})
                    if query.get("fields") == "representations":
                        assert self.headers.get("X-Rep-Hints") == "[pdf]"
                        return self._json(200, {"type": "file", "id": item["id"], "representations": {
                            "entries": [{"representation": "pdf", "properties": {},
                                         "info": {"url": f"{stub.url}/files/{item['id']}"},
                                         "status": {"state": stub.pdf_state},
                                         "content": {"url_template":
                                                     f"{stub.url}/reps/{item['id']}/{{+asset_path}}"}}]}})
                    return self._json(200, stub.view(item))
                self._not_found()

            def _slice(self, rows, query):
                offset, limit = int(query.get("offset") or 0), int(query.get("limit") or 100)
                self._json(200, {"total_count": len(rows), "offset": offset, "limit": limit,
                                 "entries": [stub.view(i) for i in rows[offset:offset + limit]]})

            def _events(self, query):
                assert query.get("stream_type") == "changes"
                end = POSITION_BASE + len(stub.events)
                if query["stream_position"] == "now":
                    return self._json(200, {"chunk_size": 0, "next_stream_position": end,
                                            "entries": []})
                start = int(query["stream_position"]) - POSITION_BASE
                chunk = stub.events[start:start + int(query.get("limit") or 100)]
                self._json(200, {"chunk_size": len(chunk), "entries": chunk,
                                 "next_stream_position": POSITION_BASE + start + len(chunk)})

            # -- OPTIONS (preflight) -------------------------------------
            def do_OPTIONS(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed(path):
                    return
                body = json.loads(self._body() or b"{}")
                existing = stub._child(body["parent"]["id"], body["name"])
                if existing is not None:
                    return self._conflict(existing, as_list=False)
                self._json(200, {"upload_url": f"{stub.url}/files/content",
                                 "upload_token": None})

            # -- POST ----------------------------------------------------
            def do_POST(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed(path):
                    return
                if path == "/files/content" or (path.startswith("/files/") and path.endswith("/content")):
                    return self._upload(path)
                body = json.loads(self._body() or b"{}")
                if path == "/folders":
                    parent = body["parent"]["id"]
                    existing = stub._child(parent, body["name"])
                    if existing is not None:
                        return self._conflict(existing, as_list=True)
                    made = {"type": "folder", "id": stub._id(), "name": body["name"],
                            "_parent": parent, "size": 0, "item_status": "active",
                            "modified_at": stub._stamp(), "owned_by": {"login": stub.ACCOUNT},
                            "modified_by": {"login": stub.ACCOUNT}}
                    stub.items[made["id"]] = made
                    stub._emit("ITEM_CREATE", made)
                    return self._json(201, stub.view(made))
                if path == "/collaborations":
                    item_id = body["item"]["id"]
                    if stub.items.get(item_id) is None:
                        return self._not_found()
                    role = body["role"]
                    collab_id = stub.collaborate(item_id, body["accessible_by"]["login"], role)
                    return self._json(201, stub.collaborations[collab_id])
                self._not_found()

            def _upload(self, path):
                head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    head + self._body())
                parts = list(message.iter_parts())
                assert parts[0].get_param("name", header="content-disposition") == "attributes", \
                    "the attributes part must come first"
                attributes = json.loads(parts[0].get_content())
                content = parts[1].get_payload(decode=True)
                stub.uploads.append(attributes["name"])
                if stub.drop_upload:
                    # The file was received; no answer ever comes.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                if path == "/files/content":
                    parent = attributes["parent"]["id"]
                    existing = stub._child(parent, attributes["name"])
                    if existing is not None:
                        return self._conflict(existing, as_list=False)
                    item = {"type": "file", "id": stub._id(), "name": attributes["name"],
                            "_parent": parent, "size": len(content), "_content": content,
                            "item_status": "active", "modified_at": stub._stamp(),
                            "owned_by": {"login": stub.ACCOUNT},
                            "modified_by": {"login": stub.ACCOUNT}}
                    stub.items[item["id"]] = item
                else:
                    item = stub.items[path.split("/")[2]]
                    item.update({"size": len(content), "_content": content})
                stub._emit("ITEM_UPLOAD", item)
                self._json(201, {"total_count": 1, "entries": [stub.view(item)]})

            # -- PUT -----------------------------------------------------
            def do_PUT(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed(path):
                    return
                parts = path.strip("/").split("/")
                item = stub._live(stub.items.get(parts[1]), parts[0][:-1]) if len(parts) == 2 else None
                if item is None:
                    return self._not_found()
                body = json.loads(self._body() or b"{}")
                if "shared_link" in body:
                    link = body["shared_link"]
                    if link is None:
                        item.pop("shared_link", None)
                        stub._emit("ITEM_SHARED_UNSHARE", item)
                    else:
                        if link["access"] == "company" and not stub.enterprise:
                            return self._error(400, "bad_request", "company access needs an enterprise")
                        item["shared_link"] = {
                            "url": f"https://app.box.com/s/sh{item['id']}x",
                            "access": link["access"],
                            "permissions": {"can_download": True, "can_preview": True,
                                            "can_edit": bool(link["permissions"].get("can_edit"))}}
                        stub._emit("ITEM_SHARED_CREATE", item)
                    return self._json(200, stub.view(item))
                parent = (body.get("parent") or {}).get("id") or item["_parent"]
                name = body.get("name") or item["name"]
                clash = stub._child(parent, name)
                if clash is not None and clash["id"] != item["id"]:
                    return self._conflict(clash, as_list=True)
                moved = parent != item["_parent"]
                item["_parent"], renamed, item["name"] = parent, name != item["name"], name
                if moved:
                    stub._emit("ITEM_MOVE", item)
                if renamed:
                    stub._emit("ITEM_RENAME", item)
                self._json(200, stub.view(item))

            # -- DELETE --------------------------------------------------
            def do_DELETE(self):  # noqa: N802
                path, query = self._route()
                if not self._authed(path):
                    return
                parts = path.strip("/").split("/")
                if parts[0] == "collaborations":
                    if stub.collaborations.pop(parts[1], None) is None:
                        return self._not_found()
                    return self._empty()
                item = stub._live(stub.items.get(parts[1]), parts[0][:-1])
                if item is None:
                    return self._not_found()
                if item["type"] == "folder" and query.get("recursive") != "true" and any(
                        i.get("_parent") == item["id"] for i in stub.items.values()):
                    return self._error(400, "folder_not_empty", "Cannot delete - folder not empty")
                stub.trash(item["id"])
                self._empty()

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
