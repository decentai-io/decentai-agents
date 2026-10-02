"""A loopback Google Docs: the Docs v1 and Drive v3 subset the Google
Docs agent calls, over real HTTP, holding a few fictional documents for
Sidra Office Supplies.

It behaves the way Google does where the agent depends on it: one
server answers both hosts' paths (``/v1/documents`` and
``/drive/v3/...``); a document body is structural elements with start
and end indexes, a paragraph ending in its newline, the body opening
with a section break; ``batchUpdate`` applies its requests in order and
all or nothing, and refuses the whole batch with a 400 when
``writeControl.requiredRevisionId`` is not the latest revision; every
content change moves the revision; Drive refuses ``orderBy`` on a
fullText query; comments come back only when ``fields`` is given;
``startModifiedTime`` filters on modified time, which a reply moves;
and a comment author carries ``me`` rather than an address.

Indexes here count Python characters, which match Google's UTF-16 units
for the text the tests use.
"""

from __future__ import annotations

import copy
import json
import re
import socket
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

DOCUMENT = "application/vnd.google-apps.document"
VALUE = r"'((?:[^'\\]|\\.)*)'"
EPOCH = datetime(2026, 9, 10, 9, 0, 0, tzinfo=timezone.utc)


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def stamp(seconds: int) -> str:
    return (EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class StubRefusal(Exception):
    pass


class GoogleDocsStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.batch_updates: List[Dict[str, Any]] = []
        self.comment_writes: List[Dict[str, Any]] = []
        self.drop_batch = False
        # Called with the document id when a batchUpdate arrives, before
        # it is judged — how a test makes someone edit in between.
        self.before_batch = None
        self.clock = 0
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def tick(self) -> str:
        self.clock += 60
        return stamp(self.clock)

    def add_document(self, title: str, blocks: List[Any]) -> str:
        """blocks: ("HEADING_1", "text"), ("NORMAL_TEXT", "text", "image"),
        or {"table": [[["cell paragraph", ...], ...], ...]}."""
        doc_id = self._id("1DoC")
        model = []
        for block in blocks:
            if isinstance(block, dict):
                model.append({"table": [[[{"style": "NORMAL_TEXT", "text": t, "image": False}
                                          for t in cell] for cell in row]
                                        for row in block["table"]]})
            else:
                model.append({"style": block[0], "text": block[1],
                              "image": len(block) > 2 and block[2] == "image"})
        self.docs[doc_id] = {"id": doc_id, "title": title, "revision": 1, "blocks": model,
                             "modified": self.tick(), "comments": []}
        return doc_id

    def text_of(self, doc_id: str) -> List[str]:
        return [p["text"] for p, _, _ in self.layout(self.docs[doc_id])[1]]

    def edit_as(self, doc_id: str, number: int, text: str) -> None:
        """Someone else changes paragraph ``number`` in the Docs editor."""
        doc = self.docs[doc_id]
        self.layout(doc)[1][number - 1][0]["text"] = text
        doc["revision"] += 1
        doc["modified"] = self.tick()

    def comment_as(self, doc_id: str, name: str, text: str, quote: str = "",
                   at: Optional[str] = None, me: bool = False) -> str:
        created = at or self.tick()
        comment = {"id": self._id("AAAAc"), "content": text,
                   "author": {"displayName": name, "me": me},
                   "createdTime": created, "modifiedTime": created,
                   "resolved": False, "deleted": False, "replies": []}
        if quote:
            comment["quotedFileContent"] = {"mimeType": "text/plain", "value": quote}
        self.docs[doc_id]["comments"].append(comment)
        return comment["id"]

    def reply_as(self, doc_id: str, comment_id: str, name: str, text: str,
                 action: str = "", me: bool = False, at: Optional[str] = None) -> str:
        comment = self.find_comment(doc_id, comment_id)
        created = at or self.tick()
        reply = {"id": self._id("AAAAr"), "author": {"displayName": name, "me": me},
                 "createdTime": created, "modifiedTime": created, "deleted": False}
        if text:
            reply["content"] = text
        if action:
            reply["action"] = action
            comment["resolved"] = action == "resolve"
        comment["replies"].append(reply)
        comment["modifiedTime"] = created
        return reply["id"]

    def find_comment(self, doc_id: str, comment_id: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.docs[doc_id]["comments"] if c["id"] == comment_id), None)

    # -- the document as Docs lays it out ------------------------------
    @staticmethod
    def layout(doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], int, int]]]:
        """The body's structural elements, and every paragraph with the
        index where its text starts and where the paragraph ends."""
        spans: List[Tuple[Dict[str, Any], int, int]] = []

        def paragraph(p, index):
            start, elements = index, []
            text = p["text"]
            if p["image"]:
                if text:
                    elements.append({"startIndex": index, "endIndex": index + len(text),
                                     "textRun": {"content": text}})
                    index += len(text)
                elements.append({"startIndex": index, "endIndex": index + 1,
                                 "inlineObjectElement": {"inlineObjectId": "kix.logo"}})
                index += 1
                elements.append({"startIndex": index, "endIndex": index + 1,
                                 "textRun": {"content": "\n"}})
                index += 1
            else:
                elements.append({"startIndex": index, "endIndex": index + len(text) + 1,
                                 "textRun": {"content": text + "\n"}})
                index += len(text) + 1
            spans.append((p, start, index))
            return {"startIndex": start, "endIndex": index,
                    "paragraph": {"elements": elements,
                                  "paragraphStyle": {"namedStyleType": p["style"]}}}, index

        content: List[Dict[str, Any]] = [{"endIndex": 1, "sectionBreak": {}}]
        index = 1
        for block in doc["blocks"]:
            if "table" in block:
                start = index
                index += 1
                rows = []
                for row in block["table"]:
                    index += 1
                    cells = []
                    for cell in row:
                        index += 1
                        cell_content = []
                        for p in cell:
                            element, index = paragraph(p, index)
                            cell_content.append(element)
                        cells.append({"content": cell_content})
                    rows.append({"tableCells": cells})
                index += 1
                content.append({"startIndex": start, "endIndex": index,
                                "table": {"rows": len(rows), "columns": len(block["table"][0]),
                                          "tableRows": rows}})
            else:
                element, index = paragraph(block, index)
                content.append(element)
        return content, spans

    def document_json(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        return {"documentId": doc["id"], "title": doc["title"],
                "revisionId": f"ALm37BV-rev{doc['revision']}",
                "body": {"content": self.layout(doc)[0]}}

    def file_json(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": doc["id"], "name": doc["title"], "mimeType": DOCUMENT,
                "modifiedTime": doc["modified"],
                "webViewLink": f"https://docs.google.com/document/d/{doc['id']}/edit",
                "lastModifyingUser": {"displayName": "Demo", "emailAddress": self.ACCOUNT}}

    def apply(self, doc: Dict[str, Any], requests_: List[Dict[str, Any]]) -> None:
        """The batch, in order, all or nothing."""
        working = copy.deepcopy(doc)
        for request in requests_:
            spans = self.layout(working)[1]
            if "deleteContentRange" in request:
                span = request["deleteContentRange"]["range"]
                a, b = int(span["startIndex"]), int(span["endIndex"])
                if a >= b:
                    raise StubRefusal("Invalid requests[0].deleteContentRange: The range cannot be empty.")
                for p, start, _ in spans:
                    if start <= a and b <= start + len(p["text"]):
                        p["text"] = p["text"][: a - start] + p["text"][b - start:]
                        break
                else:
                    raise StubRefusal("Invalid deletion range.")
            elif "insertText" in request:
                at = int(request["insertText"]["location"]["index"])
                text = request["insertText"]["text"]
                for p, start, _ in spans:
                    if start <= at <= start + len(p["text"]):
                        p["text"] = p["text"][: at - start] + text + p["text"][at - start:]
                        break
                else:
                    raise StubRefusal("The insertion index must be inside the bounds of an existing paragraph.")
            else:
                raise StubRefusal(f"unsupported request {list(request)}")
        doc["blocks"] = working["blocks"]

    # -- the server ------------------------------------------------------
    def start(self) -> "GoogleDocsStub":
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

            def _error(self, status: int, message: str, reason: str = "") -> None:
                error: Dict[str, Any] = {"code": status, "message": message}
                if reason:
                    error["status"] = reason
                self._json(status, {"error": error})

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                return (path[4:] if path.startswith("/api") else path), query

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._error(401, "Request had invalid authentication credentials.",
                                "UNAUTHENTICATED")
                    return False
                return True

            def _body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
                doc = stub.docs.get(doc_id)
                if doc is None:
                    self._error(404, f"File not found: {doc_id}.", "NOT_FOUND")
                return doc

            def _comments_view(self, doc, query):
                if not query.get("fields"):
                    return self._error(400, "The 'fields' parameter is required for this method.")
                rows = [c for c in doc["comments"]
                        if query.get("includeDeleted") == "true" or not c["deleted"]]
                since = query.get("startModifiedTime")
                if since:
                    rows = [c for c in rows if c["modifiedTime"] >= since]
                start = int(query.get("pageToken") or 0)
                size = int(query.get("pageSize") or 20)
                answer: Dict[str, Any] = {"comments": copy.deepcopy(rows[start: start + size])}
                if start + size < len(rows):
                    answer["nextPageToken"] = str(start + size)
                return self._json(200, answer)

            def do_GET(self):  # noqa: N802
                path, query = self._route()
                if not self._authed():
                    return
                if path == "/drive/v3/about":
                    return self._json(200, {"user": {"emailAddress": stub.ACCOUNT,
                                                     "displayName": "Demo"}})
                if path == "/drive/v3/files":
                    q = query.get("q", "")
                    if "fullText" in q and query.get("orderBy"):
                        return self._error(400, "Sorting is not supported for queries with "
                                                "fullText terms. Results are always in "
                                                "descending relevance order.")
                    needle = re.search(r"name contains " + VALUE, q)
                    needle = _unescape(needle.group(1)).lower() if needle else ""
                    rows = []
                    for doc in stub.docs.values():
                        body = " ".join(stub.text_of(doc["id"])).lower()
                        if needle in doc["title"].lower() or needle in body:
                            rows.append(stub.file_json(doc))
                    return self._json(200, {"files": rows[: int(query.get("pageSize") or 100)]})
                match = re.match(r"^/drive/v3/files/([^/]+)/comments/([^/]+)$", path)
                if match:
                    doc = self._doc(match.group(1))
                    if doc is None:
                        return
                    comment = stub.find_comment(doc["id"], match.group(2))
                    if comment is None:
                        return self._error(404, "Comment not found.", "NOT_FOUND")
                    return self._json(200, comment)
                match = re.match(r"^/drive/v3/files/([^/]+)(/comments)?$", path)
                if match:
                    doc = self._doc(match.group(1))
                    if doc is None:
                        return
                    if match.group(2):
                        return self._comments_view(doc, query)
                    return self._json(200, stub.file_json(doc))
                match = re.match(r"^/v1/documents/([^/:]+)$", path)
                if match:
                    doc = self._doc(match.group(1))
                    if doc is not None:
                        self._json(200, stub.document_json(doc))
                    return
                self._error(404, f"no route {path}")

            def do_POST(self):  # noqa: N802
                path, _ = self._route()
                if not self._authed():
                    return
                body = self._body()
                match = re.match(r"^/v1/documents/([^/:]+):batchUpdate$", path)
                if match:
                    doc = self._doc(match.group(1))
                    if doc is None:
                        return
                    stub.batch_updates.append(body)
                    if stub.before_batch is not None:
                        stub.before_batch(doc["id"])
                    wanted = (body.get("writeControl") or {}).get("requiredRevisionId")
                    current = f"ALm37BV-rev{doc['revision']}"
                    if wanted and wanted != current:
                        return self._error(400, f"The required revision ID ({wanted}) does not "
                                                f"match the latest revision.", "INVALID_ARGUMENT")
                    try:
                        stub.apply(doc, body.get("requests") or [])
                    except StubRefusal as exc:
                        return self._error(400, str(exc), "INVALID_ARGUMENT")
                    doc["revision"] += 1
                    doc["modified"] = stub.tick()
                    if stub.drop_batch:
                        # Applied; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    return self._json(200, {
                        "documentId": doc["id"], "replies": [{} for _ in body.get("requests") or []],
                        "writeControl": {"requiredRevisionId": f"ALm37BV-rev{doc['revision']}"}})
                match = re.match(r"^/drive/v3/files/([^/]+)/comments(?:/([^/]+)/replies)?$", path)
                if match:
                    doc = self._doc(match.group(1))
                    if doc is None:
                        return
                    stub.comment_writes.append(body)
                    if match.group(2):
                        if stub.find_comment(doc["id"], match.group(2)) is None:
                            return self._error(404, "Comment not found.", "NOT_FOUND")
                        if not body.get("content") and not body.get("action"):
                            return self._error(400, "A reply needs content or an action.")
                        reply_id = stub.reply_as(doc["id"], match.group(2), "Demo",
                                                 str(body.get("content") or ""),
                                                 action=str(body.get("action") or ""), me=True)
                        comment = stub.find_comment(doc["id"], match.group(2))
                        return self._json(200, next(r for r in comment["replies"] if r["id"] == reply_id))
                    comment_id = stub.comment_as(
                        doc["id"], "Demo", str(body.get("content") or ""),
                        quote=str((body.get("quotedFileContent") or {}).get("value") or ""), me=True)
                    return self._json(200, stub.find_comment(doc["id"], comment_id))
                self._error(404, f"no route {path}")

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
