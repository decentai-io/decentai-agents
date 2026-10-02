"""A loopback Google Drive: the v3 subset the Google Drive agent calls,
over real HTTP, holding a small fictional drive for Sidra Office
Supplies.

It behaves the way Drive does where the agent depends on it: there are
no paths, only parents, and "root" names My Drive; queries use Drive's
own little language (the stub understands the clauses the agent
writes); sizes come back as strings; a Google Doc has no bytes and is
exported; an upload is multipart/related; names are not unique in a
folder; delete is permanent, which is why the agent trashes instead —
anything deleted for good is kept here to be asserted against.
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

FOLDER = "application/vnd.google-apps.folder"
NATIVE = {"document": "application/vnd.google-apps.document",
          "spreadsheet": "application/vnd.google-apps.spreadsheet",
          "presentation": "application/vnd.google-apps.presentation"}
ROOT = "0AroOtMyDrive"
VALUE = r"'((?:[^'\\]|\\.)*)'"


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


class GoogleDriveStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.files: Dict[str, Dict[str, Any]] = {
            ROOT: {"id": ROOT, "name": "My Drive", "mimeType": FOLDER, "parents": []}}
        self.perms: Dict[str, List[Dict[str, Any]]] = {}
        self.deleted_for_good: List[str] = []
        self.uploads: List[str] = []
        self.drop_upload = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _new_id(self) -> str:
        self._n += 1
        return f"1dRv{self._n:04d}x"

    def _stamp(self, name: str, mime: str, parent: Optional[str], **extra) -> Dict[str, Any]:
        item_id = self._new_id()
        item = {"id": item_id, "name": name, "mimeType": mime,
                "parents": [parent] if parent else [], "trashed": False,
                "modifiedTime": "2026-09-01T10:00:00.000Z",
                "lastModifyingUser": {"emailAddress": self.ACCOUNT, "displayName": "Demo"},
                **extra}
        self.files[item_id] = item
        self.perms[item_id] = [{"id": "owner-" + item_id, "type": "user", "role": "owner",
                                "emailAddress": self.ACCOUNT}]
        return item

    def _put(self, parent: str, name: str, content: bytes, mime: str = "") -> Dict[str, Any]:
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        item = self._stamp(name, mime, parent, size=str(len(content)))
        item["_content"] = content
        return item

    def add_folder(self, path: str) -> str:
        parent = ROOT
        for part in [p for p in path.split("/") if p]:
            found = next((f for f in self.files.values() if parent in f["parents"]
                          and f["name"] == part and f["mimeType"] == FOLDER), None)
            parent = (found or self._stamp(part, FOLDER, parent))["id"]
        return parent

    def add_file(self, folder: str, name: str, content: bytes) -> str:
        return self._put(self.add_folder(folder), name, content)["id"]

    def add_native(self, folder: str, name: str, kind: str) -> str:
        return self._stamp(name, NATIVE[kind], self.add_folder(folder))["id"]

    def share_with_me(self, name: str, content: bytes, by: str) -> str:
        item = self._put("", name, content)
        item["parents"] = []            # it lives in their drive, not in mine
        item["_shared_with_me"] = True
        item["shared"] = True
        item["sharingUser"] = {"emailAddress": by, "displayName": by}
        return item["id"]

    def by_path(self, path: str) -> Optional[Dict[str, Any]]:
        item: Optional[Dict[str, Any]] = self.files[ROOT]
        for part in [p for p in path.split("/") if p]:
            item = next((f for f in self.files.values() if item["id"] in f["parents"]
                         and f["name"] == part and not f["trashed"]), None)
            if item is None:
                return None
        return item

    # -- Drive's query language, the clauses the agent writes ----------
    def _matches(self, item: Dict[str, Any], q: str) -> bool:
        if item["id"] == ROOT:
            return False
        if "trashed = false" in q and item.get("trashed"):
            return False
        if "sharedWithMe = true" in q and not item.get("_shared_with_me"):
            return False
        parent = re.search(VALUE + r" in parents", q)
        if parent:
            wanted = _unescape(parent.group(1))
            if (ROOT if wanted == "root" else wanted) not in item["parents"]:
                return False
        name = re.search(r"name = " + VALUE, q)
        if name and item["name"] != _unescape(name.group(1)):
            return False
        mime = re.search(r"mimeType = " + VALUE, q)
        if mime and item["mimeType"] != _unescape(mime.group(1)):
            return False
        text = re.search(r"name contains " + VALUE, q)
        if text:
            needle = _unescape(text.group(1)).lower()
            body = item.get("_content", b"").decode("utf-8", "replace").lower()
            if needle not in item["name"].lower() and needle not in body:
                return False
        return True

    def view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        kind = "drive/folders" if item["mimeType"] == FOLDER else "file/d"
        out["webViewLink"] = f"https://drive.google.com/{kind}/{item['id']}/view"
        if len(self.perms.get(item["id"], [])) > 1:
            out["shared"] = True
        return out

    # -- the server ------------------------------------------------------
    def start(self) -> "GoogleDriveStub":
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

            def _error(self, status: int, message: str) -> None:
                self._json(status, {"error": {"code": status, "message": message}})

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                return (path[4:] if path.startswith("/api") else path), query

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._error(401, "Request had invalid authentication credentials.")
                    return False
                return True

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _file(self, item_id: str) -> Optional[Dict[str, Any]]:
                return stub.files.get(ROOT if item_id == "root" else item_id)

            def do_GET(self):  # noqa: N802
                path, query = self._route()
                if not self._authed():
                    return
                if path == "/drive/v3/about":
                    used = sum(len(f.get("_content", b"")) for f in stub.files.values()
                               if not f.get("_shared_with_me"))
                    return self._json(200, {"user": {"emailAddress": stub.ACCOUNT},
                                            "storageQuota": {"limit": str(15 * 1024 ** 3),
                                                             "usage": str(used)}})
                if path == "/drive/v3/files":
                    q = query.get("q", "")
                    rows = [f for f in stub.files.values() if stub._matches(f, q)]
                    if query.get("orderBy") == "folder,name":
                        rows.sort(key=lambda f: (f["mimeType"] != FOLDER, f["name"].lower()))
                    start = int(query.get("pageToken") or 0)
                    size = int(query.get("pageSize") or 100)
                    answer: Dict[str, Any] = {"files": [stub.view(f) for f in rows[start: start + size]]}
                    if start + size < len(rows):
                        answer["nextPageToken"] = str(start + size)
                    return self._json(200, answer)
                match = re.match(r"^/drive/v3/files/([^/]+)(/export|/permissions)?$", path)
                if match:
                    item = self._file(match.group(1))
                    if item is None:
                        return self._error(404, f"File not found: {match.group(1)}.")
                    tail = match.group(2)
                    if tail == "/permissions":
                        return self._json(200, {"permissions": stub.perms.get(item["id"], [])})
                    if tail == "/export":
                        if item["mimeType"] not in NATIVE.values():
                            return self._error(403, "Export only supports Docs Editors files.")
                        wanted = query.get("mimeType", "")
                        head = b"%PDF-1.7\n% exported " if wanted == "application/pdf" else b"PK\x03\x04 exported "
                        return self._bytes(head + item["name"].encode("utf-8"))
                    if query.get("alt") == "media":
                        if "_content" not in item:
                            return self._error(403, "Only files with binary content can be "
                                                    "downloaded. Use Export with Docs Editors files.")
                        return self._bytes(item["_content"])
                    return self._json(200, stub.view(item))
                self._error(404, f"no route {path}")

            def do_POST(self):  # noqa: N802
                path, query = self._route()
                if not self._authed():
                    return
                if path == "/upload/drive/v3/files" and query.get("uploadType") == "multipart":
                    boundary = self.headers["Content-Type"].split("boundary=", 1)[1].strip('"')
                    parts = self._body().split(b"--" + boundary.encode())
                    def payload(part: bytes) -> bytes:
                        _, _, rest = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
                        return rest[:-2] if rest.endswith(b"\r\n") else rest
                    metadata = json.loads(payload(parts[1]))
                    media_head = parts[2].lstrip(b"\r\n").partition(b"\r\n\r\n")[0].decode()
                    stub.uploads.append(metadata["name"])
                    if stub.drop_upload:
                        # The file was received; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    parent = metadata["parents"][0]
                    mime = media_head.split(":", 1)[1].strip()
                    return self._json(200, stub.view(stub._put(parent, metadata["name"],
                                                               payload(parts[2]), mime)))
                if path == "/drive/v3/files":
                    body = json.loads(self._body() or b"{}")
                    parent = body["parents"][0]
                    return self._json(200, stub.view(stub._stamp(
                        body["name"], body.get("mimeType") or "application/octet-stream",
                        ROOT if parent == "root" else parent)))
                match = re.match(r"^/drive/v3/files/([^/]+)/permissions$", path)
                if match:
                    item = self._file(match.group(1))
                    if item is None:
                        return self._error(404, "File not found.")
                    body = json.loads(self._body() or b"{}")
                    permission = {"id": "perm-" + stub._new_id(), **body}
                    permission.pop("allowFileDiscovery", None)
                    stub.perms[item["id"]].append(permission)
                    return self._json(200, permission)
                self._error(404, f"no route {path}")

            def do_PATCH(self):  # noqa: N802
                path, query = self._route()
                if not self._authed():
                    return
                match = re.match(r"^/(upload/)?drive/v3/files/([^/]+)$", path)
                item = self._file(match.group(2)) if match else None
                if item is None:
                    return self._error(404, "File not found.")
                if match.group(1):
                    content = self._body()
                    item["_content"], item["size"] = content, str(len(content))
                    return self._json(200, stub.view(item))
                body = json.loads(self._body() or b"{}")
                for key in ("name", "trashed"):
                    if key in body:
                        item[key] = body[key]
                if query.get("removeParents"):
                    item["parents"] = [p for p in item["parents"]
                                       if p not in query["removeParents"].split(",")]
                if query.get("addParents"):
                    item["parents"].append(query["addParents"])
                self._json(200, stub.view(item))

            def do_DELETE(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed():
                    return
                match = re.match(r"^/drive/v3/files/([^/]+)/permissions/([^/]+)$", path)
                if match:
                    granted = stub.perms.get(match.group(1), [])
                    permission = next((p for p in granted if p["id"] == match.group(2)), None)
                    if permission is None:
                        return self._error(404, "Permission not found.")
                    if permission["role"] == "owner":
                        return self._error(403, "The owner of a file cannot be removed.")
                    granted.remove(permission)
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                match = re.match(r"^/drive/v3/files/([^/]+)$", path)
                if match and stub.files.pop(match.group(1), None) is not None:
                    stub.deleted_for_good.append(match.group(1))
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._error(404, "File not found.")

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
