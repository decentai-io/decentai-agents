"""A loopback Microsoft Graph: the mail subset the Outlook agent calls,
over real HTTP, holding fictional messages. The agent is pointed at it
through the credential's api_base_url. It can also refuse a stale
access token (an expired connection) and drop a send without
answering (an unknown outcome).

Ids are immutable, as the agent asks Graph for: a draft keeps its id
when a send moves it to Sent Items, and ``isDraft`` flips — which is
what the agent reads back to confirm the send.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def _mailbox(text: str) -> Dict[str, Any]:
    from email.utils import parseaddr

    name, addr = parseaddr(text or "")
    return {"emailAddress": {"name": name or addr, "address": addr}}


def _mailboxes(text: str) -> List[Dict[str, Any]]:
    return [_mailbox(part.strip()) for part in (text or "").split(",") if part.strip()]


def _graph_time(text: str) -> str:
    """A date as Graph returns it: ISO 8601 in UTC, to the second. The
    fixtures write RFC 2822; a string already in Graph's shape passes."""
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return text
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.attachments: Dict[str, Dict[str, Any]] = {}
        self.sends: List[str] = []
        self.token_calls = 0
        self.drop_send = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def add_message(self, conversation_id: str, sender: str, to: str, subject: str,
                    body: str, date: str, attachments=None,
                    internet_id: str = "", is_draft: bool = False) -> str:
        self._n += 1
        message_id = f"AAMk{self._n:04x}"
        internet_id = internet_id or f"<{message_id}@stub.example>"
        self.messages[message_id] = {
            "id": message_id, "conversationId": conversation_id,
            "internetMessageId": internet_id,
            "subject": subject, "bodyPreview": body[:80],
            "body": {"contentType": "text", "content": body},
            "from": _mailbox(sender), "toRecipients": _mailboxes(to),
            "ccRecipients": [],
            "receivedDateTime": _graph_time(date), "sentDateTime": _graph_time(date),
            "hasAttachments": bool(attachments), "isDraft": is_draft,
            "webLink": f"https://outlook.office365.com/owa/?ItemID={message_id}",
        }
        for entry in attachments or []:
            filename, mime, raw = entry[0], entry[1], entry[2]
            # Graph returns three shapes in one collection, and only a
            # file carries bytes: an item attachment is a whole message
            # or event, a reference attachment a link to cloud storage.
            kind = entry[3] if len(entry) > 3 else "file"
            attachment_id = f"att-{message_id}-{len(self.attachments) + 1}"
            item = {
                "message_id": message_id, "id": attachment_id, "name": filename,
                "contentType": mime, "size": len(raw), "isInline": False,
                "@odata.type": {
                    "file": "#microsoft.graph.fileAttachment",
                    "item": "#microsoft.graph.itemAttachment",
                    "reference": "#microsoft.graph.referenceAttachment",
                }[kind],
            }
            if kind == "file":
                item["contentBytes"] = base64.b64encode(raw).decode("ascii")
            self.attachments[attachment_id] = item
        self.order.append(message_id)
        return message_id

    def conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        return [self.messages[m] for m in self.order
                if self.messages[m]["conversationId"] == conversation_id
                and not self.messages[m]["isDraft"]]

    @property
    def drafts(self) -> Dict[str, Dict[str, Any]]:
        return {m: self.messages[m] for m in self.order if self.messages[m]["isDraft"]}

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphStub":
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
                return json.loads(raw or b"{}")

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                               "message": "Access token has expired."}})
                    return False
                return True

            def _parts(self):
                url = urllib.parse.urlparse(self.path)
                return url.path, urllib.parse.parse_qs(url.query)

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                path, query = self._parts()
                if path == "/api/me":
                    return self._json(200, {"mail": GraphStub.ACCOUNT,
                                            "userPrincipalName": GraphStub.ACCOUNT,
                                            "displayName": "Demo"})
                if path == "/api/me/messages":
                    return self._json(200, stub._list(query))
                if path == "/api/me/mailFolders/inbox/messages":
                    return self._json(200, stub._list(query, inbox=True))
                if path.startswith("/api/me/messages/") and "/attachments/" in path:
                    item = stub.attachments.get(path.rsplit("/", 1)[-1])
                    if item is None:
                        return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
                    return self._json(200, item)
                if path.startswith("/api/me/messages/") and path.endswith("/attachments"):
                    message_id = path.split("/")[4]
                    items = [{k: v for k, v in a.items() if k not in ("contentBytes", "message_id")}
                             for a in stub.attachments.values() if a["message_id"] == message_id]
                    return self._json(200, {"value": items})
                if path.startswith("/api/me/messages/"):
                    message = stub.messages.get(path.rsplit("/", 1)[-1])
                    if message is None:
                        return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
                    return self._json(200, message)
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                path, _ = self._parts()
                if path == "/api/me/messages":
                    body = self._payload()
                    draft_id = stub.add_message(
                        f"conv-{stub._n + 1:04x}", GraphStub.ACCOUNT,
                        ", ".join(r["emailAddress"]["address"] for r in body.get("toRecipients") or []),
                        str(body.get("subject") or ""),
                        str((body.get("body") or {}).get("content") or ""),
                        "2026-09-07T05:00:00Z", is_draft=True)
                    stub.messages[draft_id]["ccRecipients"] = body.get("ccRecipients") or []
                    return self._json(201, stub.messages[draft_id])
                if path.startswith("/api/me/messages/") and path.endswith("/createReply"):
                    source = stub.messages.get(path.split("/")[4])
                    if source is None:
                        return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
                    subject = source["subject"]
                    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                    draft_id = stub.add_message(
                        source["conversationId"], GraphStub.ACCOUNT,
                        source["from"]["emailAddress"]["address"], subject, "",
                        "2026-09-07T05:00:00Z", is_draft=True)
                    stub.messages[draft_id]["inReplyTo"] = source["internetMessageId"]
                    return self._json(201, stub.messages[draft_id])
                if path.startswith("/api/me/messages/") and path.endswith("/send"):
                    draft_id = path.split("/")[4]
                    stub.sends.append(draft_id)
                    draft = stub.messages.get(draft_id)
                    if draft is None or not draft["isDraft"]:
                        return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
                    if stub.drop_send:
                        # The request was received; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    draft["isDraft"] = False
                    draft["sentDateTime"] = draft["receivedDateTime"] = "2026-09-07T05:01:00Z"
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})

            def do_PATCH(self):  # noqa: N802
                if not self._authed():
                    return
                path, _ = self._parts()
                draft = stub.messages.get(path.rsplit("/", 1)[-1])
                if draft is None:
                    return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
                changes = self._payload()
                if "body" in changes:
                    draft["body"] = changes["body"]
                    draft["bodyPreview"] = str(changes["body"].get("content") or "")[:80]
                for key in ("toRecipients", "ccRecipients", "subject"):
                    if key in changes:
                        draft[key] = changes[key]
                return self._json(200, draft)

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                path, _ = self._parts()
                draft_id = path.rsplit("/", 1)[-1]
                if stub.messages.pop(draft_id, None) is None:
                    return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "Not found"}})
                stub.order.remove(draft_id)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

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

    # -- listing: a conversation filter, a time filter, or a KQL-ish search
    def _list(self, query: Dict[str, List[str]],
              inbox: bool = False) -> Dict[str, Any]:
        top = int(query.get("$top", ["10"])[0])
        skip = int(query.get("$skip", ["0"])[0] or 0)
        filter_ = query.get("$filter", [""])[0]
        orderby = query.get("$orderby", [""])[0]
        if filter_.startswith("conversationId eq "):
            conversation_id = filter_[len("conversationId eq "):].strip("'").replace("''", "'")
            return {"value": self.conversation(conversation_id)}
        if filter_.startswith("receivedDateTime ge ") or orderby.startswith("receivedDateTime"):
            # The inbox holds what arrived, not what we sent. Times are
            # Graph's fixed ISO shape, so strings order as instants do;
            # arrival order breaks ties, as Graph's does.
            since = filter_[len("receivedDateTime ge "):].strip() if filter_ else ""
            pool = [m for m in self.order
                    if not self.messages[m]["isDraft"]
                    and not (inbox and self.messages[m]["from"]["emailAddress"]["address"] == self.ACCOUNT)
                    and self.messages[m]["receivedDateTime"] >= since]
            pool.sort(key=lambda m: (self.messages[m]["receivedDateTime"], self.order.index(m)),
                      reverse=orderby.endswith("desc"))
            return {"value": [self.messages[m] for m in pool[:top]]}

        terms = query.get("$search", [""])[0].strip('"').split()
        hits = [m for m in reversed(self.order)
                if not self.messages[m]["isDraft"] and self._matches(self.messages[m], terms)]
        page = hits[skip: skip + top]
        answer: Dict[str, Any] = {"value": [self.messages[m] for m in page],
                                  "@odata.count": len(hits)}
        if skip + top < len(hits):
            params = {"$search": query.get("$search", [""])[0], "$top": top, "$skip": skip + top}
            answer["@odata.nextLink"] = f"{self.url}/api/me/messages?{urllib.parse.urlencode(params)}"
        return answer

    @staticmethod
    def _matches(message: Dict[str, Any], terms: List[str]) -> bool:
        sender = message["from"]["emailAddress"]["address"].lower()
        subject = message["subject"].lower()
        text = message["body"]["content"].lower()
        for term in terms:
            low = term.lower()
            if low in ("and", "or"):
                continue
            if low.startswith("from:"):
                if low[5:] not in sender:
                    return False
            elif low.startswith("subject:"):
                if low[8:] not in subject:
                    return False
            elif low.startswith("received") or low.startswith("hasattachments:"):
                continue
            elif low not in subject and low not in text and low not in sender:
                return False
        return True
