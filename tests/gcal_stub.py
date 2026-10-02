"""A loopback Google Calendar: the subset the Google Calendar agent calls, over
real HTTP, holding fictional events for Sidra Office Supplies. Attendee
calendars are readable only when shared; an unshared one answers
free/busy with an error, exactly as Google does.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def _iso(text: str) -> datetime:
    text = str(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class CalendarStub:
    ACCOUNT = "demo@sidra.example"
    TIMEZONE = "Asia/Dubai"

    def __init__(self):
        self.events: Dict[str, Dict[str, Any]] = {}
        self.shared: Dict[str, List[Dict[str, str]]] = {}   # email -> busy periods
        self.inserts: List[Dict[str, Any]] = []
        self.expired = False
        self.drop_insert = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    def add_event(self, summary: str, start: str, end: str, attendees=None) -> str:
        self._n += 1
        event_id = f"ev{self._n:03d}"
        self.events[event_id] = {
            "id": event_id, "status": "confirmed", "summary": summary,
            "start": {"dateTime": start, "timeZone": self.TIMEZONE},
            "end": {"dateTime": end, "timeZone": self.TIMEZONE},
            "organizer": {"email": self.ACCOUNT},
            "attendees": [{"email": a, "responseStatus": "needsAction"}
                          for a in attendees or []],
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        }
        return event_id

    def share(self, email: str, busy: List[Dict[str, str]]) -> None:
        self.shared[email] = busy

    def _busy_of(self, email: str, time_min: str, time_max: str) -> Optional[List[Dict[str, str]]]:
        if email == self.ACCOUNT:
            periods = [{"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]}
                       for e in self.events.values() if e["status"] == "confirmed"]
        elif email in self.shared:
            periods = self.shared[email]
        else:
            return None
        lo, hi = _iso(time_min), _iso(time_max)
        return [p for p in periods if _iso(p["end"]) > lo and _iso(p["start"]) < hi]

    def start(self) -> "CalendarStub":
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

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"message": "Invalid Credentials"}})
                    return False
                return True

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/token":
                    form = self._payload()
                    if stub.expired or form.get("refresh_token") == "expired":
                        return self._json(400, {"error": "invalid_grant"})
                    return self._json(200, {"access_token": "at-1", "expires_in": 3600})
                if not self._authed():
                    return
                if path == "/api/freeBusy":
                    body = self._payload()
                    calendars = {}
                    for item in body.get("items") or []:
                        email = item["id"]
                        busy = stub._busy_of(email, body["timeMin"], body["timeMax"])
                        calendars[email] = ({"busy": busy} if busy is not None else
                                            {"busy": [], "errors": [{"domain": "global", "reason": "notFound"}]})
                    return self._json(200, {"calendars": calendars})
                if path == "/api/calendars/primary/events":
                    event = self._payload()
                    stub.inserts.append(event)
                    if stub.drop_insert:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    event_id = stub.add_event(
                        event.get("summary") or "", event["start"]["dateTime"],
                        event["end"]["dateTime"],
                        [a["email"] for a in event.get("attendees") or []])
                    stored = stub.events[event_id]
                    for field in ("description", "location"):
                        if event.get(field):
                            stored[field] = event[field]
                    return self._json(200, stored)
                self._json(404, {"error": {"message": f"no route {path}"}})

            def do_PATCH(self):  # noqa: N802
                if not self._authed():
                    return
                path = urllib.parse.urlparse(self.path).path
                event = stub.events.get(path.rsplit("/", 1)[-1])
                if event is None:
                    return self._json(404, {"error": {"message": "Not Found"}})
                event.update(self._payload())
                self._json(200, event)

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                path = urllib.parse.urlparse(self.path).path
                event = stub.events.get(path.rsplit("/", 1)[-1])
                if event is None:
                    return self._json(404, {"error": {"message": "Not Found"}})
                event["status"] = "cancelled"
                self.send_response(204)
                self.end_headers()

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                path = url.path
                if path == "/api/calendars/primary":
                    return self._json(200, {"id": stub.ACCOUNT, "timeZone": stub.TIMEZONE})
                if path == "/api/calendars/primary/events":
                    lo, hi = _iso(query["timeMin"][0]), _iso(query["timeMax"][0])
                    q = str(query.get("q", [""])[0]).lower()
                    rows = sorted(
                        (e for e in stub.events.values()
                         if e["status"] == "confirmed"
                         and _iso(e["end"]["dateTime"]) > lo
                         and _iso(e["start"]["dateTime"]) < hi
                         and (not q or q in e["summary"].lower())),
                        key=lambda e: e["start"]["dateTime"])
                    limit = int(query.get("maxResults", ["25"])[0])
                    offset = int(query.get("pageToken", ["0"])[0] or 0)
                    page = rows[offset: offset + limit]
                    answer: Dict[str, Any] = {"items": page}
                    if offset + limit < len(rows):
                        answer["nextPageToken"] = str(offset + limit)
                    return self._json(200, answer)
                if path.startswith("/api/calendars/primary/events/"):
                    event = stub.events.get(path.rsplit("/", 1)[-1])
                    if event is None:
                        return self._json(404, {"error": {"message": "Not Found"}})
                    return self._json(200, event)
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
