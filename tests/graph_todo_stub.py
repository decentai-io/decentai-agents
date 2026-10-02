"""A loopback Microsoft Graph: the To Do subset the Microsoft To Do agent
calls, over real HTTP, holding fictional tasks for Sidra Office
Supplies. The agent is pointed at it through the credential's
api_base_url. It refuses a stale access token (an expired connection).

Its clock is the test's: every change stamps lastModifiedDateTime with
``clock`` plus Graph's seven fraction digits, so a test decides which
changes share a second. Ids carry "/" and "=", as real To Do ids can,
so an unescaped id in a path fails here the way it would there.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def _moment(text: str) -> datetime:
    text = re.sub(r"\.\d+", "", str(text))
    if text.endswith("Z"):
        text = text[:-1]
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class GraphTodoStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.lists: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[str, List[Dict[str, Any]]] = {}      # list id -> tasks, in creation order
        self.checklists: Dict[str, List[Dict[str, Any]]] = {}  # task id -> items
        self.writes: List[str] = []
        self.clock = "2026-09-10T08:00:00"
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def stamp(self) -> str:
        self._n += 1
        return f"{self.clock}.{self._n:07d}Z"

    def add_list(self, name: str, wellknown: str = "none") -> str:
        list_id = f"AQMkAD/list={len(self.lists) + 1:02d}"
        self.lists[list_id] = {"id": list_id, "displayName": name, "isOwner": True,
                               "isShared": False, "wellknownListName": wellknown}
        self.tasks[list_id] = []
        return list_id

    def add_task(self, list_id: str, title: str, *, due: str = "", note: str = "",
                 importance: str = "normal", status: str = "notStarted") -> str:
        self._n += 1
        task_id = f"AAMkAG/task={self._n:04d}"
        moment = self.stamp()
        task: Dict[str, Any] = {
            "@odata.etag": f'W/"{self._n}"', "id": task_id, "title": title,
            "status": status, "importance": importance, "isReminderOn": False,
            "createdDateTime": moment, "lastModifiedDateTime": moment,
            "body": {"content": note, "contentType": "text"},
        }
        if due:
            task["dueDateTime"] = {"dateTime": f"{due}T00:00:00.0000000", "timeZone": "UTC"}
        if status == "completed":
            task["completedDateTime"] = {"dateTime": f"{self.clock[:10]}T00:00:00.0000000",
                                         "timeZone": "UTC"}
        self.tasks[list_id].append(task)
        return task_id

    def task(self, task_id: str) -> Optional[Dict[str, Any]]:
        for rows in self.tasks.values():
            for task in rows:
                if task["id"] == task_id:
                    return task
        return None

    def complete_in_app(self, task_id: str) -> None:
        """The person ticks a task off in To Do itself."""
        self._apply(self.task(task_id), {"status": "completed"})

    def edit_in_app(self, task_id: str, **changes: Any) -> None:
        self._apply(self.task(task_id), changes)

    def _apply(self, task: Dict[str, Any], changes: Dict[str, Any]) -> None:
        for key, value in changes.items():
            if value is None:
                task.pop(key, None)
            else:
                task[key] = value
        if changes.get("status") == "completed":
            # To Do keeps the day it was completed, as midnight.
            task["completedDateTime"] = {"dateTime": f"{self.clock[:10]}T00:00:00.0000000",
                                         "timeZone": "UTC"}
        elif "status" in changes:
            task.pop("completedDateTime", None)
        task["lastModifiedDateTime"] = self.stamp()

    # -- querying, as Graph does ---------------------------------------
    def _select(self, list_id: str, query: Dict[str, List[str]]):
        rows = list(self.tasks[list_id])
        filter_ = query.get("$filter", [""])[0]
        for clause in [c.strip() for c in filter_.split(" and ") if c.strip()]:
            match = re.fullmatch(r"(\S+) (eq|ne|ge|lt) (.+)", clause)
            if not match:
                return None, f"cannot read filter {clause!r}"
            field, op, value = match.groups()
            value = value.strip("'")
            if field == "status":
                rows = [t for t in rows if (t["status"] == value) == (op == "eq")]
            elif field == "lastModifiedDateTime" and op == "ge":
                rows = [t for t in rows if _moment(t["lastModifiedDateTime"]) >= _moment(value)]
            elif field == "dueDateTime/dateTime" and op == "lt":
                rows = [t for t in rows if t.get("dueDateTime")
                        and t["dueDateTime"]["dateTime"][:19] < value[:19]]
            else:
                return None, f"unsupported filter {clause!r}"
        order = query.get("$orderby", [""])[0]
        if order:
            field, _, direction = order.partition(" ")
            if field != "lastModifiedDateTime":
                return None, f"unsupported orderby {order!r}"
            rows.sort(key=lambda t: _moment(t["lastModifiedDateTime"]),
                      reverse=direction == "desc")
        return rows, ""

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphTodoStub":
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

            def _empty(self) -> None:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                               "message": "Access token has expired."}})
                    return False
                return True

            def _missing(self):
                self._json(404, {"error": {"code": "ErrorItemNotFound",
                                           "message": "The specified object was not found in the store."}})

            def _route(self):
                """(segments after /api, query) — each segment unescaped,
                so an id holding "/" must have arrived as %2F."""
                url = urllib.parse.urlparse(self.path)
                parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
                assert parts[:1] == ["api"], url.path
                return parts[1:], urllib.parse.parse_qs(url.query)

            def _task(self, parts):
                if len(parts) < 6 or parts[3] not in stub.lists:
                    return None, None
                for task in stub.tasks[parts[3]]:
                    if task["id"] == parts[5]:
                        return parts[3], task
                return parts[3], None

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                parts, query = self._route()
                if parts == ["me"]:
                    return self._json(200, {"mail": stub.ACCOUNT, "userPrincipalName": stub.ACCOUNT,
                                            "displayName": "Demo"})
                if parts == ["me", "todo", "lists"]:
                    return self._json(200, {"value": list(stub.lists.values())})
                if parts[:3] == ["me", "todo", "lists"] and len(parts) == 4:
                    row = stub.lists.get(parts[3])
                    return self._json(200, row) if row else self._missing()
                if parts[:3] == ["me", "todo", "lists"] and len(parts) == 5 and parts[4] == "tasks":
                    if parts[3] not in stub.lists:
                        return self._missing()
                    rows, problem = stub._select(parts[3], query)
                    if problem:
                        return self._json(400, {"error": {"code": "BadRequest", "message": problem}})
                    top = int(query.get("$top", ["100"])[0])
                    skip = int(query.get("$skip", ["0"])[0] or 0)
                    answer: Dict[str, Any] = {"value": rows[skip: skip + top]}
                    if skip + top < len(rows):
                        params = {k: v[0] for k, v in query.items()}
                        params.update({"$top": top, "$skip": skip + top})
                        answer["@odata.nextLink"] = (
                            f"{stub.url}/api/me/todo/lists/{urllib.parse.quote(parts[3], safe='')}"
                            f"/tasks?{urllib.parse.urlencode(params)}")
                    return self._json(200, answer)
                if len(parts) == 6 and parts[4] == "tasks":
                    _, task = self._task(parts)
                    return self._json(200, task) if task else self._missing()
                if len(parts) == 7 and parts[6] == "checklistItems":
                    _, task = self._task(parts)
                    if task is None:
                        return self._missing()
                    return self._json(200, {"value": stub.checklists.get(task["id"], [])})
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {parts}"}})

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                parts, _ = self._route()
                body = self._payload()
                if parts == ["me", "todo", "lists"]:
                    stub.writes.append("create list")
                    list_id = stub.add_list(str(body.get("displayName") or ""))
                    return self._json(201, stub.lists[list_id])
                if parts[:3] == ["me", "todo", "lists"] and len(parts) == 5 and parts[4] == "tasks":
                    if parts[3] not in stub.lists:
                        return self._missing()
                    stub.writes.append("create task")
                    task_id = stub.add_task(parts[3], str(body.get("title") or ""))
                    task = stub.task(task_id)
                    for key in ("importance", "body", "dueDateTime", "reminderDateTime",
                                "isReminderOn"):
                        if key in body:
                            task[key] = body[key]
                    return self._json(201, task)
                self._json(404, {"error": {"code": "BadRequest", "message": f"no route {parts}"}})

            def do_PATCH(self):  # noqa: N802
                if not self._authed():
                    return
                parts, _ = self._route()
                _, task = self._task(parts)
                if task is None:
                    return self._missing()
                stub.writes.append(f"patch {task['id']}")
                stub._apply(task, self._payload())
                return self._json(200, task)

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                parts, _ = self._route()
                list_id, task = self._task(parts)
                if task is None:
                    return self._missing()
                stub.writes.append(f"delete {task['id']}")
                stub.tasks[list_id].remove(task)
                self._empty()

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
