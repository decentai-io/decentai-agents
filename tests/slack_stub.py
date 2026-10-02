"""A loopback Slack Web API: the methods the Slack agent calls, over real
HTTP, holding a fictional Sidra Office Supplies workspace.

It behaves the way Slack does where the agent depends on it: every
answer is HTTP 200 with an ``ok`` envelope, failures included (a
revoked token is ``{"ok": false, "error": "token_revoked"}``); only a
rate limit is HTTP 429 with Retry-After. Lists page by
``response_metadata.next_cursor``; search pages by page number. History
comes newest first and ``oldest`` is exclusive; a thread's replies come
parent first, oldest first. Message ts values are strings of seconds
with six decimals, unique within a conversation.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

TEAM_URL = "https://sidra-office.slack.com/"
ME = "U0DEMO"
DANA = "U0DANA"
OMAR = "U0OMAR"
PRIYA = "U0PRIYA"
PEOPLE = {
    ME: ("demo", "Demo", "demo@sidra.example"),
    DANA: ("dana", "Dana Harbour", "dana@sidra.example"),
    OMAR: ("omar", "Omar Nasser", "omar@sidra.example"),
    PRIYA: ("priya", "Priya Menon", "priya@sidra.example"),
}
GENERAL = "C0GENERAL"
SHOWROOM = "C0SHOWROOM"
FINANCE = "G0FINANCE"
DM_DANA = "D0DANA"
DM_OMAR = "D0OMAR"
GROUP = "G0MPIMOPS"

GOOD_TOKEN = "xoxp-demo"
REVOKED_TOKEN = "xoxp-revoked"


class SlackStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.members: Dict[str, List[str]] = {}
        self.posts: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.drop_send = False
        self.rate_limit = 0          # answer this many calls with 429
        self._clock = Decimal(int(time.time()) - 3600)
        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        self.add_conversation(GENERAL, "general", is_channel=True)
        self.add_conversation(SHOWROOM, "showroom", is_channel=True)
        self.add_conversation(FINANCE, "finance", is_private=True, is_group=True)
        self.add_conversation(DM_DANA, DANA, is_im=True, user=DANA)
        self.add_conversation(DM_OMAR, OMAR, is_im=True, user=OMAR)
        self.add_conversation(GROUP, "mpdm-demo--omar--priya-1", is_mpim=True,
                              members=[ME, OMAR, PRIYA])
        chairs = self.add_message(SHOWROOM, ME, "Has Northlight confirmed the chair delivery?")
        self.add_message(SHOWROOM, OMAR, "Not yet, chasing them today.", thread_ts=chairs)
        self.add_message(SHOWROOM, DANA, "Harbourline want the layout signed off &amp; the "
                                         "drawings by Thursday.")
        self.add_message(GENERAL, PRIYA, "Office closed on the 24th for the move.")
        self.add_message(GENERAL, PRIYA, "", subtype="channel_join")
        self.add_message(DM_DANA, DANA, "Can you send the showroom drawings?")
        self.add_message(GROUP, OMAR, f"<@{ME}> can you check the Cloudledger export?")
        self.chairs_ts = chairs

    # -- fictional data ------------------------------------------------------
    def add_conversation(self, channel: str, name: str, members: Optional[List[str]] = None,
                         **flags: Any) -> None:
        self.conversations[channel] = {
            "id": channel, "name": name, "created": 1700000000,
            "updated": int(self._clock * 1000),
            "is_channel": False, "is_group": False, "is_im": False, "is_mpim": False,
            "is_private": False, **flags}
        self.messages.setdefault(channel, [])
        self.members[channel] = members or ([ME, flags["user"]] if flags.get("user")
                                            else list(PEOPLE))

    def add_message(self, channel: str, user: str, text: str, thread_ts: str = "",
                    subtype: str = "", now: bool = False) -> str:
        with self._lock:
            # Seeded messages a second apart an hour ago; "now" messages
            # on the real clock, so a watch started now sees them, and
            # never earlier than anything before.
            if now:
                self._clock = max(self._clock + Decimal("0.000001"),
                                  Decimal(f"{time.time():.6f}"))
            else:
                self._clock += Decimal("1.000100")
            ts = f"{self._clock:.6f}"
        message: Dict[str, Any] = {"type": "message", "user": user, "text": text, "ts": ts}
        if subtype:
            message["subtype"] = subtype
        if thread_ts:
            message["thread_ts"] = thread_ts
            parent = self.find(channel, thread_ts)
            parent["thread_ts"] = thread_ts
            parent["reply_count"] = int(parent.get("reply_count") or 0) + 1
        self.messages[channel].append(message)
        self.conversations[channel]["updated"] = int(Decimal(ts) * 1000)
        return ts

    def say(self, channel: str, user: str, text: str, thread_ts: str = "") -> str:
        """Someone writes now, in real time."""
        return self.add_message(channel, user, text, thread_ts=thread_ts, now=True)

    def find(self, channel: str, ts: str) -> Dict[str, Any]:
        return next(m for m in self.messages[channel] if m["ts"] == ts)

    # -- the server ----------------------------------------------------------
    def start(self) -> "SlackStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, body: Any, status: int = 200, headers=None) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _fail(self, error: str) -> None:
                self._json({"ok": False, "error": error})

            def _args(self) -> Dict[str, Any]:
                url = urllib.parse.urlparse(self.path)
                args: Dict[str, Any] = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw = self.rfile.read(length)
                    if "json" in (self.headers.get("Content-Type") or ""):
                        args.update(json.loads(raw or b"{}"))
                    else:
                        args.update({k: v[0] for k, v in
                                     urllib.parse.parse_qs(raw.decode("utf-8")).items()})
                return args

            def do_GET(self):  # noqa: N802
                self._serve()

            def do_POST(self):  # noqa: N802
                self._serve()

            def _serve(self) -> None:
                path = urllib.parse.urlparse(self.path).path
                method = path.rsplit("/", 1)[-1]
                args = self._args()
                stub.calls.append(method)
                if stub.rate_limit > 0:
                    stub.rate_limit -= 1
                    return self._json({"ok": False, "error": "ratelimited"}, 429,
                                      {"Retry-After": "30"})
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if not token:
                    return self._fail("not_authed")
                if token == REVOKED_TOKEN:
                    return self._fail("token_revoked")
                if token != GOOD_TOKEN:
                    return self._fail("invalid_auth")
                handler = getattr(self, "m_" + method.replace(".", "_"), None)
                if handler is None:
                    return self._fail("unknown_method")
                handler(args)

            # -- methods ---------------------------------------------------
            def m_auth_test(self, args):
                self._json({"ok": True, "url": TEAM_URL, "team": "Sidra Office Supplies",
                            "user": "demo", "team_id": "T0SIDRA", "user_id": ME})

            def _page(self, rows, args, key, **extra):
                limit = int(args.get("limit") or 100)
                start = int(args.get("cursor") or 0)
                chunk = rows[start:start + limit]
                cursor = str(start + limit) if start + limit < len(rows) else ""
                self._json({"ok": True, key: chunk, "has_more": bool(cursor),
                            "response_metadata": {"next_cursor": cursor}, **extra})

            def m_users_conversations(self, args):
                wanted = set(str(args.get("types") or "public_channel").split(","))
                def kind(c):
                    if c["is_im"]:
                        return "im"
                    if c["is_mpim"]:
                        return "mpim"
                    return "private_channel" if c["is_private"] else "public_channel"
                rows = [c for c in stub.conversations.values()
                        if kind(c) in wanted and ME in stub.members[c["id"]]]
                self._page(rows, args, "channels")

            def m_conversations_info(self, args):
                conversation = stub.conversations.get(str(args.get("channel")))
                if conversation is None:
                    return self._fail("channel_not_found")
                self._json({"ok": True, "channel": conversation})

            def m_conversations_members(self, args):
                if args.get("channel") not in stub.conversations:
                    return self._fail("channel_not_found")
                self._page(stub.members[args["channel"]], args, "members")

            def m_conversations_history(self, args):
                channel = str(args.get("channel"))
                if channel not in stub.conversations:
                    return self._fail("channel_not_found")
                oldest = Decimal(str(args.get("oldest") or "0"))
                latest = Decimal(str(args.get("latest") or "99999999999"))
                rows = [m for m in stub.messages[channel]
                        if oldest < Decimal(m["ts"]) < latest
                        and (not m.get("thread_ts") or m["thread_ts"] == m["ts"])]
                rows.sort(key=lambda m: Decimal(m["ts"]), reverse=True)
                self._page(rows, args, "messages")

            def m_conversations_replies(self, args):
                channel = str(args.get("channel"))
                if channel not in stub.conversations:
                    return self._fail("channel_not_found")
                ts = str(args.get("ts"))
                target = next((m for m in stub.messages[channel] if m["ts"] == ts), None)
                if target is None:
                    return self._fail("thread_not_found")
                root = target.get("thread_ts") or ts
                if root != ts:
                    # Slack answers a reply's ts with that reply alone.
                    return self._json({"ok": True, "messages": [target], "has_more": False})
                rows = [m for m in stub.messages[channel]
                        if m["ts"] == ts or m.get("thread_ts") == ts]
                rows.sort(key=lambda m: Decimal(m["ts"]))
                self._page(rows, args, "messages")

            def m_search_messages(self, args):
                query = str(args.get("query") or "").lower()
                count = int(args.get("count") or 20)
                page = int(args.get("page") or 1)
                matches = []
                for channel, messages in stub.messages.items():
                    if ME not in stub.members[channel]:
                        continue
                    conversation = stub.conversations[channel]
                    for m in messages:
                        if m.get("subtype") or query not in m["text"].lower():
                            continue
                        link = f"{TEAM_URL}archives/{channel}/p{m['ts'].replace('.', '')}"
                        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                            link += f"?thread_ts={m['thread_ts']}&cid={channel}"
                        matches.append({
                            "type": "message", "user": m["user"], "username": PEOPLE[m["user"]][0],
                            "text": m["text"], "ts": m["ts"], "permalink": link,
                            "channel": {"id": channel, "name": conversation["name"],
                                        "is_im": conversation["is_im"],
                                        "is_mpim": conversation["is_mpim"],
                                        "is_private": conversation["is_private"]}})
                matches.sort(key=lambda m: Decimal(m["ts"]), reverse=True)
                pages = max(1, -(-len(matches) // count))
                chunk = matches[(page - 1) * count: page * count]
                self._json({"ok": True, "query": args.get("query"), "messages": {
                    "total": len(matches), "matches": chunk,
                    "paging": {"count": count, "total": len(matches), "page": page,
                               "pages": pages}}})

            def m_users_info(self, args):
                user = PEOPLE.get(str(args.get("user")))
                if user is None:
                    return self._fail("user_not_found")
                self._json({"ok": True, "user": {
                    "id": args["user"], "name": user[0], "real_name": user[1],
                    "profile": {"display_name": user[1], "real_name": user[1],
                                "email": user[2]}}})

            def m_chat_postMessage(self, args):  # noqa: N802
                channel = str(args.get("channel"))
                if channel not in stub.conversations:
                    return self._fail("channel_not_found")
                stub.posts.append(dict(args))
                ts = stub.add_message(channel, ME, str(args.get("text")),
                                      thread_ts=str(args.get("thread_ts") or ""), now=True)
                if stub.drop_send:
                    # The message was posted; no answer ever comes.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self._json({"ok": True, "channel": channel, "ts": ts,
                            "message": stub.find(channel, ts)})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, access_token: str = GOOD_TOKEN) -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": "demo", "access_token": access_token,
                "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
