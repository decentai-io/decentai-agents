"""A loopback Microsoft Graph for Word files: the OneDrive subset the
Word Online agent calls, over real HTTP, holding real .docx bytes that
the tests build with python-docx.

It behaves the way Graph does where the agent depends on it: there is
no content API for a Word document, only the file; a download answers
with a redirect to a pre-signed address; every save moves the item's
quoted ``eTag`` and records who saved it and when; a ``PUT .../content``
whose ``If-Match`` is not the current eTag is refused with 412 and
changes nothing; search matches names and returns files of every kind
in no useful order.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

EPOCH = datetime(2026, 9, 10, 9, 0, 0, tzinfo=timezone.utc)
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class GraphWordStub:
    ACCOUNT = "demo@sidra.example"
    DISPLAY_NAME = "Demo User"

    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {}
        self.downloads: List[str] = []
        self.puts: List[Dict[str, Any]] = []
        self.drop_put = False
        # Called with the item id when a PUT arrives, before If-Match is
        # judged — how a test makes someone save in between.
        self.before_put = None
        self.clock = 0
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def tick(self) -> str:
        self.clock += 60
        return (EPOCH + timedelta(seconds=self.clock)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_file(self, name: str, raw: bytes, by: str = ACCOUNT) -> str:
        self._n += 1
        item_id = f"01WRD{self._n:04d}"
        self.items[item_id] = {"id": item_id, "name": name, "version": 0}
        self.save_as(item_id, raw, by)
        return item_id

    def save_as(self, item_id: str, raw: bytes, by: str) -> None:
        """Someone saves new bytes (in Word, or through the API)."""
        item = self.items[item_id]
        item["content"] = raw
        item["version"] += 1
        item["modified"] = self.tick()
        item["by"] = by

    def etag(self, item_id: str) -> str:
        return f'"{{3F2504E0-4F89-11D3-9A0C-{item_id[-4:]:0>12}}},{self.items[item_id]["version"]}"'

    def view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        facet = {"mimeType": DOCX} if item["name"].endswith(".docx") else {"mimeType": "application/pdf"}
        return {"id": item["id"], "name": item["name"], "size": len(item["content"]),
                "eTag": self.etag(item["id"]), "cTag": f'"c:{item["id"]},{item["version"]}"',
                "lastModifiedDateTime": item["modified"],
                "lastModifiedBy": {"user": {"email": item["by"], "displayName": item["by"]}},
                "webUrl": "https://sidra-my.sharepoint.com/personal/demo/Documents/"
                          + urllib.parse.quote(item["name"]),
                "file": facet, "parentReference": {"driveId": "b!demo", "path": "/drive/root:"}}

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphWordStub":
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

            def _error(self, status: int, code: str, message: str) -> None:
                self._json(status, {"error": {"code": code, "message": message}})

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                return (path[4:] if path.startswith("/api") else path), urllib.parse.parse_qs(url.query)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._error(401, "InvalidAuthenticationToken", "Access token has expired.")
                    return False
                return True

            def do_GET(self):  # noqa: N802
                path, query = self._route()
                if path.startswith("/download/"):
                    # The pre-signed address a content request redirects to.
                    item_id = path.split("/")[2]
                    stub.downloads.append(item_id)
                    raw = stub.items[item_id]["content"]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if not self._authed():
                    return
                if path == "/me":
                    return self._json(200, {"mail": stub.ACCOUNT, "userPrincipalName": stub.ACCOUNT,
                                            "displayName": stub.DISPLAY_NAME})
                match = re.match(r"^/me/drive/root/search\(q='(.*)'\)$", path)
                if match:
                    q = match.group(1).replace("''", "'").lower()
                    rows = [stub.view(i) for i in stub.items.values() if q in i["name"].lower()]
                    return self._json(200, {"value": rows[: int(query.get("$top", ["20"])[0])]})
                match = re.match(r"^/me/drive/items/([^/]+)(/content)?$", path)
                if match:
                    item = stub.items.get(match.group(1))
                    if item is None:
                        return self._error(404, "itemNotFound", "The resource could not be found.")
                    if match.group(2):
                        self.send_response(302)
                        self.send_header("Location", f"{stub.url}/download/{item['id']}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._json(200, stub.view(item))
                self._error(404, "BadRequest", f"no route {path}")

            def do_PUT(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed():
                    return
                match = re.match(r"^/me/drive/items/([^/]+)/content$", path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not match or match.group(1) not in stub.items:
                    return self._error(404, "itemNotFound", "The resource could not be found.")
                item_id = match.group(1)
                stub.puts.append({"item_id": item_id, "if_match": self.headers.get("If-Match"),
                                  "size": len(raw)})
                if stub.before_put is not None:
                    stub.before_put(item_id)
                wanted = self.headers.get("If-Match")
                if wanted and wanted != stub.etag(item_id):
                    return self._error(412, "resourceModified",
                                       "ETag does not match current item's value")
                stub.save_as(item_id, raw, stub.ACCOUNT)
                if stub.drop_put:
                    # Saved; no answer ever comes.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self._json(200, stub.view(stub.items[item_id]))

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
