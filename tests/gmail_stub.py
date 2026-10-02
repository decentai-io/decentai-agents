"""A loopback Gmail: the subset of the API the Gmail agent calls, over
real HTTP, holding fictional messages. The agent is pointed at it
through the credential's token_url and api_base_url. It can also be
told to refuse the refresh token (an expired connection) and to drop a
send without answering (an unknown outcome).
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _internal_date(text: str) -> str:
    """When a message arrived, as Gmail reports it: milliseconds since
    the epoch, as a string. The fixtures write RFC 2822 dates."""
    from email.utils import parsedate_to_datetime

    try:
        return str(int(parsedate_to_datetime(text).timestamp()) * 1000)
    except (TypeError, ValueError):
        return "0"


class GmailStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.drafts: Dict[str, Dict[str, Any]] = {}
        self.attachments: Dict[str, bytes] = {}
        self.sends: List[str] = []
        self.token_calls = 0
        self.expired = False
        self.drop_send = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def add_message(self, thread_id: str, sender: str, to: str, subject: str,
                    body: str, date: str, attachments=None,
                    internet_id: str = "") -> str:
        self._n += 1
        message_id = f"m{self._n:04x}"
        internet_id = internet_id or f"<{message_id}@stub.example>"
        parts = [{"mimeType": "text/plain",
                  "body": {"data": _b64url(body.encode("utf-8"))}}]
        for filename, mime, raw in attachments or []:
            attachment_id = f"att-{message_id}-{len(parts)}"
            self.attachments[attachment_id] = raw
            parts.append({"filename": filename, "mimeType": mime,
                          "body": {"attachmentId": attachment_id, "size": len(raw)}})
        self.messages[message_id] = {
            "id": message_id, "threadId": thread_id,
            "internalDate": _internal_date(date),
            "snippet": body[:80],
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "To", "value": to},
                    {"name": "Date", "value": date},
                    {"name": "Subject", "value": subject},
                    {"name": "Message-ID", "value": internet_id},
                ],
                "parts": parts,
            },
        }
        self.order.append(message_id)
        return message_id

    def thread(self, thread_id: str) -> List[Dict[str, Any]]:
        return [self.messages[m] for m in self.order
                if self.messages[m]["threadId"] == thread_id]

    # -- the server ------------------------------------------------------
    def start(self) -> "GmailStub":
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

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    return json.loads(raw or b"{}")
                return dict(urllib.parse.parse_qsl(raw.decode("utf-8")))

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/token":
                    stub.token_calls += 1
                    form = self._payload()
                    if stub.expired or form.get("refresh_token") == "expired":
                        return self._json(400, {"error": "invalid_grant",
                                                "error_description": "Token has been expired or revoked."})
                    return self._json(200, {"access_token": "at-1", "expires_in": 3600})
                if not self._authed():
                    return
                if path == "/api/drafts":
                    message = self._payload().get("message") or {}
                    stub._n += 1
                    draft_id = f"r{stub._n:04x}"
                    parsed = email.message_from_bytes(
                        _unb64url(str(message.get("raw") or "")),
                        policy=email.policy.default)
                    stub.drafts[draft_id] = {
                        "id": draft_id, "threadId": str(message.get("threadId") or ""),
                        "to": parsed["To"] or "", "subject": parsed["Subject"] or "",
                        "in_reply_to": parsed["In-Reply-To"] or "",
                        "body": parsed.get_content() if parsed.get_content_type() == "text/plain" else "",
                    }
                    return self._json(200, {"id": draft_id, "message": {"id": f"d-{draft_id}"}})
                if path == "/api/drafts/send":
                    # Gmail's real shape: the draft id travels in the body.
                    draft_id = str(self._payload().get("id") or "")
                    stub.sends.append(draft_id)
                    draft = stub.drafts.get(draft_id)
                    if draft is None:
                        return self._json(404, {"error": {"message": "Draft not found"}})
                    if stub.drop_send:
                        # The request was received; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    thread_id = draft["threadId"] or f"t-{draft_id}"
                    message_id = stub.add_message(
                        thread_id, GmailStub.ACCOUNT, draft["to"], draft["subject"],
                        draft["body"], "Mon, 07 Sep 2026 09:00:00 +0400")
                    del stub.drafts[draft_id]
                    return self._json(200, {"id": message_id, "threadId": thread_id,
                                            "labelIds": ["SENT"]})
                self._json(404, {"error": {"message": f"no route {path}"}})

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                path = urllib.parse.urlparse(self.path).path
                draft_id = path.rsplit("/", 1)[-1]
                if stub.drafts.pop(draft_id, None) is None:
                    return self._json(404, {"error": {"message": "Draft not found"}})
                self.send_response(204)
                self.end_headers()

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"message": "Invalid Credentials"}})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                path = url.path
                if path == "/api/profile":
                    return self._json(200, {"emailAddress": GmailStub.ACCOUNT})
                if path == "/api/messages":
                    return self._json(200, stub._search(query))
                if path.startswith("/api/messages/") and "/attachments/" in path:
                    attachment_id = path.rsplit("/", 1)[-1]
                    raw = stub.attachments.get(attachment_id)
                    if raw is None:
                        return self._json(404, {"error": {"message": "Not Found"}})
                    return self._json(200, {"size": len(raw), "data": _b64url(raw)})
                if path.startswith("/api/messages/"):
                    message = stub.messages.get(path.rsplit("/", 1)[-1])
                    if message is None:
                        return self._json(404, {"error": {"message": "Not Found"}})
                    return self._json(200, message)
                if path.startswith("/api/threads/"):
                    thread_id = path.rsplit("/", 1)[-1]
                    messages = stub.thread(thread_id)
                    if not messages:
                        return self._json(404, {"error": {"message": "Not Found"}})
                    return self._json(200, {"id": thread_id, "messages": messages})
                self._json(404, {"error": {"message": f"no route {path}"}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, access_token: str = "at-1") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent: the
        account, a fresh access token, and the loopback base URL."""
        return {"account": self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- search: a few Gmail operators, enough for the demo ----------------
    def _search(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        terms = str(query.get("q", [""])[0]).split()
        limit = int(query.get("maxResults", ["10"])[0])
        offset = int(query.get("pageToken", ["0"])[0] or 0)

        def matches(message: Dict[str, Any]) -> bool:
            headers = {h["name"].lower(): h["value"].lower()
                       for h in message["payload"]["headers"]}
            text = _unb64url(message["payload"]["parts"][0]["body"]["data"]).decode().lower()
            for term in terms:
                low = term.lower()
                if low.startswith("from:"):
                    if low[5:] not in headers.get("from", ""):
                        return False
                elif low.startswith("subject:"):
                    if low[8:] not in headers.get("subject", ""):
                        return False
                elif low == "in:inbox":
                    # The inbox holds what arrived, not what we sent.
                    if self.ACCOUNT in headers.get("from", ""):
                        return False
                elif low.startswith("after:"):
                    if int(message["internalDate"]) < int(low[6:]) * 1000:
                        return False
                elif low.startswith("newer_than:") or low.startswith("has:"):
                    continue
                elif low not in headers.get("subject", "") and low not in text \
                        and low not in headers.get("from", ""):
                    return False
            return True

        # Newest first, as Gmail lists; arrival order breaks ties.
        newest_first = sorted(
            self.order, key=lambda m: (int(self.messages[m]["internalDate"]),
                                       self.order.index(m)), reverse=True)
        hits = [m for m in newest_first if matches(self.messages[m])]
        page = hits[offset: offset + limit]
        answer: Dict[str, Any] = {
            "messages": [{"id": m, "threadId": self.messages[m]["threadId"]} for m in page],
            "resultSizeEstimate": len(hits),
        }
        if offset + limit < len(hits):
            answer["nextPageToken"] = str(offset + limit)
        return answer
