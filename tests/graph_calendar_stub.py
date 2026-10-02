"""A loopback Microsoft Graph: the calendar subset the Microsoft
Calendar agent calls, over real HTTP, holding fictional events for
Sidra Office Supplies.

It behaves the way Graph does where the agent depends on it: times are
kept and answered in UTC with Graph's seven fractional digits; the
mailbox reports its zone as a Windows name, as most Microsoft 365
mailboxes do; event ids contain "/" and "=", so a client that does not
escape them breaks here as it would there; a colleague's calendar
answers free/busy, and an address outside the organization answers
with an error.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def _utc(text: str) -> datetime:
    """An ISO moment as aware UTC; one without an offset is UTC, which is
    how Graph's own seven-digit dateTimes arrive (the fraction dropped)."""
    text = str(text).replace("Z", "+00:00")
    if len(text) > 19 and text[19] == ".":
        text = text[:19] + text[19:].lstrip(".0123456789")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _graph_time(moment: datetime) -> Dict[str, str]:
    return {"dateTime": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".0000000",
            "timeZone": "UTC"}


def _from_request(value: Dict[str, Any]) -> datetime:
    """A {dateTime, timeZone} the agent sent; it always sends UTC."""
    assert value.get("timeZone") == "UTC", value
    return datetime.fromisoformat(str(value["dateTime"])).replace(tzinfo=timezone.utc)


class GraphCalendarStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.mailbox_zone = "Arabian Standard Time"
        self.events: Dict[str, Dict[str, Any]] = {}
        self.shared: Dict[str, List[Dict[str, str]]] = {}   # address -> busy periods
        self.creates: List[Dict[str, Any]] = []
        self.drop_create = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def add_event(self, subject: str, start: str, end: str, attendees=None) -> str:
        self._n += 1
        event_id = f"AAMkAGev/{self._n:03d}=="
        self.events[event_id] = {
            "id": event_id, "subject": subject,
            "start": _graph_time(_utc(start)), "end": _graph_time(_utc(end)),
            "isAllDay": False, "isCancelled": False, "showAs": "busy",
            "location": {"displayName": ""},
            "organizer": {"emailAddress": {"address": self.ACCOUNT}},
            "attendees": [{"emailAddress": {"address": a}, "type": "required",
                           "status": {"response": "none"}} for a in attendees or []],
            "webLink": f"https://outlook.office365.com/calendar/item/{urllib.parse.quote(event_id, safe='')}",
        }
        return event_id

    def share(self, address: str, busy: List[Dict[str, str]]) -> None:
        self.shared[address] = busy

    def _overlapping(self, lo: datetime, hi: datetime) -> List[Dict[str, Any]]:
        return sorted((e for e in self.events.values()
                       if not e["isCancelled"]
                       and _utc(e["end"]["dateTime"]) > lo and _utc(e["start"]["dateTime"]) < hi),
                      key=lambda e: e["start"]["dateTime"])

    def _schedule(self, address: str, lo: datetime, hi: datetime) -> Dict[str, Any]:
        if address.lower() == self.ACCOUNT:
            items = [{"status": "busy", "start": e["start"], "end": e["end"]}
                     for e in self._overlapping(lo, hi)]
        elif address in self.shared:
            items = [{"status": "busy", "start": _graph_time(_utc(p["start"])),
                      "end": _graph_time(_utc(p["end"]))}
                     for p in self.shared[address]
                     if _utc(p["end"]) > lo and _utc(p["start"]) < hi]
        else:
            return {"scheduleId": address,
                    "error": {"message": "The specified object was not found in the store.",
                              "responseCode": "ErrorItemNotFound"}}
        return {"scheduleId": address, "availabilityView": "", "scheduleItems": items}

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphCalendarStub":
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
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                               "message": "Access token has expired."}})
                    return False
                return True

            def _event_id(self, path: str) -> str:
                return urllib.parse.unquote(path.rsplit("/", 1)[-1])

            def _missing(self):
                self._json(404, {"error": {"code": "ErrorItemNotFound",
                                           "message": "The specified object was not found in the store."}})

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                path = url.path
                if path == "/api/me":
                    return self._json(200, {"mail": stub.ACCOUNT, "userPrincipalName": stub.ACCOUNT,
                                            "displayName": "Demo"})
                if path == "/api/me/mailboxSettings":
                    return self._json(200, {"timeZone": stub.mailbox_zone})
                if path == "/api/me/calendarView":
                    lo, hi = _utc(query["startDateTime"][0]), _utc(query["endDateTime"][0])
                    rows = stub._overlapping(lo, hi)
                    top = int(query.get("$top", ["25"])[0])
                    skip = int(query.get("$skip", ["0"])[0] or 0)
                    answer: Dict[str, Any] = {"value": rows[skip: skip + top]}
                    if skip + top < len(rows):
                        params = {"startDateTime": query["startDateTime"][0],
                                  "endDateTime": query["endDateTime"][0],
                                  "$top": top, "$skip": skip + top}
                        answer["@odata.nextLink"] = (f"{stub.url}/api/me/calendarView?"
                                                     f"{urllib.parse.urlencode(params)}")
                    return self._json(200, answer)
                if path.startswith("/api/me/events/"):
                    event = stub.events.get(self._event_id(path))
                    return self._json(200, event) if event else self._missing()
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/me/calendar/getSchedule":
                    body = self._payload()
                    lo, hi = _from_request(body["startTime"]), _from_request(body["endTime"])
                    return self._json(200, {"value": [stub._schedule(a, lo, hi)
                                                      for a in body.get("schedules") or []]})
                if path == "/api/me/events":
                    body = self._payload()
                    stub.creates.append(body)
                    if stub.drop_create:
                        # The request was received; no answer ever comes.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    event_id = stub.add_event(
                        body.get("subject") or "",
                        _from_request(body["start"]).isoformat(),
                        _from_request(body["end"]).isoformat(),
                        [a["emailAddress"]["address"] for a in body.get("attendees") or []])
                    stored = stub.events[event_id]
                    if body.get("location"):
                        stored["location"] = body["location"]
                    return self._json(201, stored)
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {path}"}})

            def do_PATCH(self):  # noqa: N802
                if not self._authed():
                    return
                path = urllib.parse.urlparse(self.path).path
                event = stub.events.get(self._event_id(path))
                if event is None:
                    return self._missing()
                changes = self._payload()
                for key in ("start", "end"):
                    if key in changes:
                        event[key] = _graph_time(_from_request(changes[key]))
                for key in ("subject", "location", "body"):
                    if key in changes:
                        event[key] = changes[key]
                self._json(200, event)

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                path = urllib.parse.urlparse(self.path).path
                if stub.events.pop(self._event_id(path), None) is None:
                    return self._missing()
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
        """The credential as the platform hands it to the agent."""
        return {"account": self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
