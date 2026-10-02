"""A loopback Microsoft Graph: the OneDrive subset the OneDrive agent
calls, over real HTTP, holding a small fictional drive for Sidra Office
Supplies.

It behaves the way Graph does where the agent depends on it: a download
answers with a redirect to a pre-signed address; an upload that meets a
name already taken is renamed "name 1.ext" unless told otherwise; a
file someone else shared lives in their drive, whose id carries a "!";
a Word file converts to PDF on request and a PDF does not; the owner's
permission cannot be removed; delete goes to the recycle bin.
"""

from __future__ import annotations

import json
import mimetypes
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

ROOT = "01ROOT"
MY_DRIVE = "b!demo"
ITEM = re.compile(r"^/(?:me/drive|drives/([^/]+))/items/([^/:]+)"
                  r"(/children|/content|/permissions|/invite|/createLink|/permissions/[^/]+)?$")


class GraphDriveStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {
            ROOT: {"id": ROOT, "name": "root", "root": {}, "folder": {}, "_parent": None}}
        self.permissions: Dict[str, List[Dict[str, Any]]] = {}
        self.recycle: List[Dict[str, Any]] = []
        self.remote: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.shared_entries: List[Dict[str, Any]] = []
        self.uploads: List[str] = []
        self.drop_upload = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _new_id(self) -> str:
        self._n += 1
        return f"01DRV{self._n:04d}"

    def _stamp(self, item_id: str, name: str, parent: str, **facets) -> Dict[str, Any]:
        item = {"id": item_id, "name": name, "_parent": parent,
                "lastModifiedDateTime": "2026-09-01T10:00:00Z",
                "lastModifiedBy": {"user": {"email": self.ACCOUNT, "displayName": "Demo"}},
                **facets}
        self.items[item_id] = item
        self.permissions[item_id] = [{"id": "owner", "roles": ["owner"],
                                      "grantedToV2": {"user": {"email": self.ACCOUNT,
                                                               "displayName": "Demo"}}}]
        return item

    def _put(self, parent: str, name: str, content: bytes) -> Dict[str, Any]:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        item = self._stamp(self._new_id(), name, parent,
                           file={"mimeType": mime}, size=len(content))
        item["_content"] = content
        return item

    def add_folder(self, path: str) -> str:
        parent = ROOT
        for part in [p for p in path.split("/") if p]:
            found = self._child(parent, part)
            if found is None:
                found = self._stamp(self._new_id(), part, parent, folder={})
            parent = found["id"]
        return parent

    def add_file(self, folder: str, name: str, content: bytes) -> str:
        return self._put(self.add_folder(folder), name, content)["id"]

    def share_with_me(self, name: str, content: bytes, by: str):
        owner = by.split("@")[0]
        drive = f"b!{owner}"
        item_id = f"01REM{len(self.shared_entries) + 1:04d}"
        item = {"id": item_id, "name": name, "size": len(content),
                "file": {"mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream"},
                "webUrl": f"https://sidra-my.sharepoint.com/personal/{owner}/Documents/"
                          f"{urllib.parse.quote(name)}",
                "parentReference": {"driveId": drive},
                "shared": {"sharedBy": {"user": {"email": by, "displayName": by}},
                           "sharedDateTime": "2026-09-05T08:00:00Z"},
                "lastModifiedDateTime": "2026-09-05T08:00:00Z", "_content": content}
        self.remote.setdefault(drive, {})[item_id] = item
        self.shared_entries.append(item)
        return drive, item_id

    # -- the drive -------------------------------------------------------
    def _child(self, parent: str, name: str) -> Optional[Dict[str, Any]]:
        return next((i for i in self.items.values()
                     if i.get("_parent") == parent and i["name"].lower() == name.lower()), None)

    def path_of(self, item_id: str) -> str:
        parts = []
        while item_id and item_id != ROOT:
            item = self.items[item_id]
            parts.append(item["name"])
            item_id = item["_parent"]
        return "/" + "/".join(reversed(parts))

    def by_path(self, path: str) -> Optional[Dict[str, Any]]:
        item: Optional[Dict[str, Any]] = self.items[ROOT]
        for part in [p for p in path.split("/") if p]:
            item = self._child(item["id"], part)
            if item is None:
                return None
        return item

    def view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        parent = item.get("_parent")
        if parent:
            out["parentReference"] = {
                "driveId": MY_DRIVE, "id": parent,
                "path": "/drive/root:" + ("" if parent == ROOT else self.path_of(parent))}
        if item["id"] != ROOT:
            out["webUrl"] = ("https://sidra-my.sharepoint.com/personal/demo/Documents"
                             + urllib.parse.quote(self.path_of(item["id"])))
        if len(self.permissions.get(item["id"], [])) > 1:
            out["shared"] = {"scope": "users"}
        return out

    def free_name(self, parent: str, name: str) -> str:
        if self._child(parent, name) is None:
            return name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        n = 1
        while True:
            candidate = f"{stem} {n}" + (f".{ext}" if ext else "")
            if self._child(parent, candidate) is None:
                return candidate
            n += 1

    def find(self, drive: Optional[str], item_id: str) -> Optional[Dict[str, Any]]:
        if drive:
            return self.remote.get(drive, {}).get(item_id)
        return self.items.get(item_id)

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphDriveStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _bytes(self, raw: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                return (path[4:] if path.startswith("/api") else path), urllib.parse.parse_qs(url.query)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                               "message": "Access token has expired."}})
                    return False
                return True

            def _missing(self):
                self._json(404, {"error": {"code": "itemNotFound",
                                           "message": "The resource could not be found."}})

            def _taken(self):
                self._json(409, {"error": {"code": "nameAlreadyExists",
                                           "message": "The specified item name already exists."}})

            def do_GET(self):  # noqa: N802
                path, query = self._route()
                if path.startswith("/download/"):
                    # The pre-signed address a content request redirects to.
                    _, _, drive, item_id = path.split("/", 3)
                    item = stub.find(None if drive == "me" else drive, item_id)
                    body = item["_content"]
                    if query.get("format") == ["pdf"]:
                        body = b"%PDF-1.7\n% converted from " + item["name"].encode("utf-8")
                    return self._bytes(body)
                if not self._authed():
                    return
                if path == "/me":
                    return self._json(200, {"mail": stub.ACCOUNT, "userPrincipalName": stub.ACCOUNT})
                if path == "/me/drive":
                    used = sum(len(i.get("_content", b"")) for i in stub.items.values())
                    return self._json(200, {"id": MY_DRIVE, "driveType": "business",
                                            "quota": {"used": used, "total": 1024 ** 4}})
                if path == "/me/drive/root":
                    return self._json(200, stub.view(stub.items[ROOT]))
                if path.startswith("/me/drive/root:"):
                    item = stub.by_path(path[len("/me/drive/root:"):])
                    return self._json(200, stub.view(item)) if item else self._missing()
                match = re.match(r"^/me/drive/root/search\(q='(.*)'\)$", path)
                if match:
                    q = match.group(1).replace("''", "'").lower()
                    rows = [stub.view(i) for i in stub.items.values()
                            if i["id"] != ROOT and q in i["name"].lower()]
                    return self._json(200, {"value": rows[: int(query.get("$top", ["20"])[0])]})
                if path == "/me/drive/sharedWithMe":
                    return self._json(200, {"value": [
                        {"id": e["id"], "name": e["name"],
                         "remoteItem": {k: v for k, v in e.items() if not k.startswith("_")}}
                        for e in stub.shared_entries]})
                match = ITEM.match(path)
                if match:
                    drive, item_id, tail = match.groups()
                    item = stub.find(drive, item_id)
                    if item is None:
                        return self._missing()
                    if not tail:
                        return self._json(200, {k: v for k, v in item.items() if not k.startswith("_")}
                                          if drive else stub.view(item))
                    if tail == "/children":
                        rows = sorted((i for i in stub.items.values() if i.get("_parent") == item_id),
                                      key=lambda i: i["name"].lower())
                        top = int(query.get("$top", ["50"])[0])
                        skip = int(query.get("$skip", ["0"])[0])
                        answer: Dict[str, Any] = {"value": [stub.view(i) for i in rows[skip: skip + top]]}
                        if skip + top < len(rows):
                            answer["@odata.nextLink"] = (f"{stub.url}/api/me/drive/items/{item_id}"
                                                         f"/children?$top={top}&$skip={skip + top}")
                        return self._json(200, answer)
                    if tail == "/content":
                        fmt = query.get("format", [""])[0]
                        if fmt == "pdf" and not item["name"].lower().endswith((".docx", ".xlsx", ".pptx")):
                            return self._json(406, {"error": {
                                "code": "notSupported",
                                "message": "Conversion to pdf is not supported for this file type."}})
                        self.send_response(302)
                        self.send_header("Location", f"{stub.url}/download/{drive or 'me'}/{item_id}"
                                                     + ("?format=pdf" if fmt == "pdf" else ""))
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if tail == "/permissions":
                        return self._json(200, {"value": stub.permissions.get(item_id, [])})
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})

            def do_PUT(self):  # noqa: N802
                path, query = self._route()
                if not self._authed():
                    return
                match = re.match(r"^/me/drive/items/([^/:]+):/([^/]+):/content$", path)
                if not match:
                    return self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})
                parent, name = match.groups()
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.uploads.append(name)
                if stub.drop_upload:
                    # The file was received; no answer ever comes.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                if parent not in stub.items:
                    return self._missing()
                behavior = query.get("@microsoft.graph.conflictBehavior", ["fail"])[0]
                existing = stub._child(parent, name)
                if existing is not None:
                    if behavior == "replace":
                        existing["_content"], existing["size"] = raw, len(raw)
                        return self._json(200, stub.view(existing))
                    if behavior == "fail":
                        return self._taken()
                    name = stub.free_name(parent, name)
                return self._json(201, stub.view(stub._put(parent, name, raw)))

            def do_POST(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed():
                    return
                match = ITEM.match(path)
                if not match or match.group(1):
                    return self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})
                _, item_id, tail = match.groups()
                if item_id not in stub.items:
                    return self._missing()
                body = self._payload()
                if tail == "/children":
                    if stub._child(item_id, body["name"]) is not None:
                        return self._taken()
                    return self._json(201, stub.view(
                        stub._stamp(stub._new_id(), body["name"], item_id, folder={})))
                if tail == "/invite":
                    granted = []
                    for recipient in body.get("recipients") or []:
                        permission = {"id": f"perm-{stub._new_id()}", "roles": body.get("roles") or ["read"],
                                      "grantedToV2": {"user": {"email": recipient["email"],
                                                               "displayName": recipient["email"]}}}
                        stub.permissions[item_id].append(permission)
                        granted.append(permission)
                    return self._json(200, {"value": granted})
                if tail == "/createLink":
                    permission_id = f"perm-{stub._new_id()}"
                    permission = {"id": permission_id,
                                  "roles": ["write" if body.get("type") == "edit" else "read"],
                                  "link": {"type": body.get("type"), "scope": body.get("scope"),
                                           "webUrl": f"https://sidra-my.sharepoint.com/:b:/g/personal/demo/{permission_id}"}}
                    stub.permissions[item_id].append(permission)
                    return self._json(201, permission)
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})

            def do_PATCH(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed():
                    return
                match = ITEM.match(path)
                item = stub.items.get(match.group(2)) if match and not match.group(3) else None
                if item is None:
                    return self._missing()
                changes = self._payload()
                parent = (changes.get("parentReference") or {}).get("id") or item["_parent"]
                name = changes.get("name") or item["name"]
                if parent not in stub.items:
                    return self._missing()
                clash = stub._child(parent, name)
                if clash is not None and clash["id"] != item["id"]:
                    return self._taken()
                item["_parent"], item["name"] = parent, name
                self._json(200, stub.view(item))

            def do_DELETE(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed():
                    return
                match = ITEM.match(path)
                if not match or match.group(1):
                    return self._missing()
                _, item_id, tail = match.groups()
                if tail and tail.startswith("/permissions/"):
                    permission_id = tail[len("/permissions/"):]
                    granted = stub.permissions.get(item_id, [])
                    permission = next((p for p in granted if p["id"] == permission_id), None)
                    if permission is None:
                        return self._missing()
                    if "owner" in permission["roles"]:
                        return self._json(403, {"error": {"code": "accessDenied",
                                                          "message": "The owner's permission cannot be removed."}})
                    granted.remove(permission)
                    return self._empty(204)
                item = stub.items.pop(item_id, None)
                if item is None:
                    return self._missing()
                stub.recycle.append(item)
                self._empty(204)

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
                "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
