"""A loopback Microsoft Graph: the Teams subset the Teams agent calls,
over real HTTP, holding fictional chats for Sidra Office Supplies.

It behaves the way Graph does where the agent depends on it: chat and
channel ids look like "19:…@thread.v2" and must be escaped in a path;
message bodies come back as HTML; a chat holds system notices as well
as messages; a message's sender is a user id, whose address is found
among the chat's members; asking for a one-to-one chat that exists
returns it rather than a second one; messages list newest first.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PEOPLE = {
    "demo@sidra.example": ("u-demo", "Demo"),
    "dana@sidra.example": ("u-dana", "Dana Harbour"),
    "omar@sidra.example": ("u-omar", "Omar Nasser"),
    "priya@sidra.example": ("u-priya", "Priya Menon"),
}
DM_DANA = "19:u-demo_u-dana@unq.gbl.spaces"
OPS = "19:ops-group@thread.v2"
TEAM = "team-sidra"
GENERAL = "19:general@thread.tacv2"
SHOWROOM = "19:showroom@thread.tacv2"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + f"{moment.microsecond // 1000:03d}Z"


class GraphTeamsStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.chats: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.posts: List[Dict[str, Any]] = []
        self.sends: List[Dict[str, Any]] = []
        self.drop_send = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self.add_chat(DM_DANA, "oneOnOne", None, ["demo@sidra.example", "dana@sidra.example"],
                      "2026-09-09T15:00:00Z")
        self.add_chat(OPS, "group", "Ops", ["demo@sidra.example", "omar@sidra.example",
                                            "priya@sidra.example"], "2026-09-10T07:30:00Z")
        self.add_message(DM_DANA, "dana@sidra.example",
                         "<p>Can you send the showroom drawings by <b>Thursday</b>?</p>"
                         "<p>Harbourline &amp; I want to sign off the layout.</p>",
                         "2026-09-09T14:58:00Z")
        self.system(DM_DANA, "2026-09-09T14:59:00Z")
        self.add_message(OPS, "omar@sidra.example",
                         "<p>Northlight moved the chairs to the 24th.</p>", "2026-09-10T07:30:00Z")
        self.teams = [{"id": TEAM, "displayName": "Sidra Office", "description": "Everyone at Sidra"}]
        self.channels = {TEAM: [{"id": GENERAL, "displayName": "General", "membershipType": "standard"},
                                {"id": SHOWROOM, "displayName": "Showroom", "membershipType": "standard"}]}

    # -- fictional data ------------------------------------------------
    def add_chat(self, chat_id: str, kind: str, topic: Optional[str], people: List[str],
                 updated: str) -> None:
        self.chats[chat_id] = {
            "id": chat_id, "chatType": kind, "topic": topic, "lastUpdatedDateTime": updated,
            "webUrl": f"https://teams.microsoft.com/l/chat/{urllib.parse.quote(chat_id, safe='')}/0",
            "members": [{"@odata.type": "#microsoft.graph.aadUserConversationMember",
                         "userId": PEOPLE[p][0], "email": p, "displayName": PEOPLE[p][1]}
                        for p in people]}
        self.messages.setdefault(chat_id, [])

    def add_message(self, chat_id: str, sender: str, content: str, at: str = "",
                    content_type: str = "html") -> Dict[str, Any]:
        self._n += 1
        at = at or _stamp(datetime.now(timezone.utc))
        user_id, name = PEOPLE[sender]
        message = {"id": f"{1757400000000 + self._n}", "messageType": "message",
                   "createdDateTime": at, "deletedDateTime": None, "chatId": chat_id,
                   "from": {"user": {"id": user_id, "displayName": name,
                                     "userIdentityType": "aadUser"}},
                   "body": {"contentType": content_type, "content": content},
                   "webUrl": f"https://teams.microsoft.com/l/message/{urllib.parse.quote(chat_id, safe='')}/{self._n}"}
        self.messages.setdefault(chat_id, []).append(message)
        if chat_id in self.chats:
            self.chats[chat_id]["lastUpdatedDateTime"] = at
        return message

    def reply(self, chat_id: str, sender: str, text: str) -> None:
        """Someone answers, a moment after the latest message."""
        latest = max(datetime.fromisoformat(m["createdDateTime"].replace("Z", "+00:00"))
                     for m in self.messages[chat_id])
        self.add_message(chat_id, sender, f"<p>{text}</p>", _stamp(latest + timedelta(seconds=5)))

    def system(self, chat_id: str, at: str) -> None:
        self._n += 1
        self.messages[chat_id].append({
            "id": f"{1757400000000 + self._n}", "messageType": "systemEventMessage",
            "createdDateTime": at, "from": None,
            "body": {"contentType": "html", "content": "<systemEventMessage/>"}})

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphTeamsStub":
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

            def _missing(self, what: str = "resource") -> None:
                self._json(404, {"error": {"code": "NotFound", "message": f"No such {what}."}})

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                raw = url.path[4:] if url.path.startswith("/api") else url.path
                # Split before unquoting: an escaped id is one segment.
                parts = [urllib.parse.unquote(p) for p in raw.strip("/").split("/")]
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                return parts, query

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                               "message": "Access token has expired."}})
                    return False
                return True

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def do_GET(self):  # noqa: N802
                parts, query = self._route()
                if not self._authed():
                    return
                if parts == ["me"]:
                    return self._json(200, {"id": "u-demo", "mail": stub.ACCOUNT,
                                            "userPrincipalName": stub.ACCOUNT, "displayName": "Demo"})
                if parts == ["me", "chats"]:
                    rows = list(stub.chats.values())[: int(query.get("$top", 50))]
                    return self._json(200, {"value": rows})
                if len(parts) == 2 and parts[0] == "chats":
                    chat = stub.chats.get(parts[1])
                    return self._json(200, chat) if chat else self._missing("chat")
                if len(parts) == 3 and parts[0] == "chats" and parts[2] == "messages":
                    if parts[1] not in stub.chats:
                        return self._missing("chat")
                    rows = sorted(stub.messages[parts[1]], key=lambda m: m["createdDateTime"],
                                  reverse=True)
                    return self._json(200, {"value": rows[: int(query.get("$top", 20))]})
                if parts == ["me", "joinedTeams"]:
                    return self._json(200, {"value": stub.teams})
                if len(parts) == 3 and parts[0] == "teams" and parts[2] == "channels":
                    if parts[1] not in stub.channels:
                        return self._missing("team")
                    return self._json(200, {"value": stub.channels[parts[1]]})
                self._missing("route")

            def do_POST(self):  # noqa: N802
                parts, _ = self._route()
                if not self._authed():
                    return
                body = self._payload()
                if parts == ["chats"]:
                    people = [m["user@odata.bind"].split("users('", 1)[1].rstrip("')")
                              for m in body.get("members") or []]
                    if any(p not in PEOPLE for p in people):
                        return self._missing("user")
                    ids = {PEOPLE[p][0] for p in people}
                    for chat in stub.chats.values():
                        if chat["chatType"] == "oneOnOne" and {m["userId"] for m in chat["members"]} == ids:
                            return self._json(201, chat)
                    chat_id = "19:" + "_".join(sorted(ids)) + "@unq.gbl.spaces"
                    stub.add_chat(chat_id, "oneOnOne", None, people, _stamp(datetime.now(timezone.utc)))
                    return self._json(201, stub.chats[chat_id])
                if len(parts) == 3 and parts[0] == "chats" and parts[2] == "messages":
                    if parts[1] not in stub.chats:
                        return self._missing("chat")
                    stub.sends.append({"chat_id": parts[1], **body})
                    if stub.drop_send:
                        # The message arrived; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    made = stub.add_message(parts[1], stub.ACCOUNT, body["body"]["content"],
                                            content_type=body["body"]["contentType"])
                    return self._json(201, made)
                if len(parts) == 5 and parts[0] == "teams" and parts[2] == "channels" and parts[4] == "messages":
                    if parts[3] not in {c["id"] for c in stub.channels.get(parts[1], [])}:
                        return self._missing("channel")
                    stub._n += 1
                    made = {"id": f"{1757400000000 + stub._n}", "createdDateTime": _stamp(datetime.now(timezone.utc)),
                            "subject": body.get("subject"), "body": body["body"],
                            "webUrl": f"https://teams.microsoft.com/l/message/{urllib.parse.quote(parts[3], safe='')}/{stub._n}"}
                    stub.posts.append({"team_id": parts[1], "channel_id": parts[3], **made})
                    return self._json(201, made)
                self._missing("route")

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
