"""A loopback Google Tasks API: the subset the Google Tasks agent calls,
over real HTTP, holding fictional tasks for Sidra Office Supplies. The
agent is pointed at it through the credential's api_base_url. It
refuses a stale access token (an expired connection).

Behaviour kept from the real service, because the agent depends on it:
tasks come back in the person's own order, never by time; completed
tasks are left out unless showCompleted, hidden ones unless showHidden;
a task ticked off in Google's own apps is hidden as well as completed;
updatedMin and completedMin bound by ``updated`` and ``completed``; and
pages are small, so an agent that does not follow nextPageToken misses
tasks. Its clock is the test's: every change stamps ``clock`` plus
milliseconds, so a test decides which changes share a second.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PAGE_CAP = 3        # the real cap is 100; a small one proves paging is followed


def _moment(text: str) -> datetime:
    text = re.sub(r"\.\d+", "", str(text))
    if text.endswith("Z"):
        text = text[:-1]
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class TasksStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.lists: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[str, List[Dict[str, Any]]] = {}   # list id -> tasks, in position order
        self.writes: List[str] = []
        self.clock = "2026-09-10T08:00:00"
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def stamp(self) -> str:
        self._n += 1
        return f"{self.clock}.{self._n % 1000:03d}Z"

    def add_list(self, title: str) -> str:
        list_id = f"MTc0NjQ3-list{len(self.lists) + 1:02d}"
        self.lists[list_id] = {"kind": "tasks#taskList", "id": list_id, "title": title,
                               "updated": self.stamp()}
        self.tasks[list_id] = []
        return list_id

    def add_task(self, list_id: str, title: str, *, due: str = "", notes: str = "",
                 parent: str = "", status: str = "needsAction") -> str:
        self._n += 1
        task_id = f"dGFzay0{self._n:04d}"
        task: Dict[str, Any] = {
            "kind": "tasks#task", "id": task_id, "title": title, "updated": self.stamp(),
            "status": status, "position": f"{len(self.tasks[list_id]):020d}",
            "webViewLink": f"https://tasks.google.com/task/{task_id}",
        }
        if notes:
            task["notes"] = notes
        if due:
            task["due"] = f"{due}T00:00:00.000Z"
        if parent:
            task["parent"] = parent
        if status == "completed":
            task["completed"] = task["updated"]
            task["hidden"] = True
        self.tasks[list_id].append(task)
        return task_id

    def task(self, task_id: str) -> Optional[Dict[str, Any]]:
        for rows in self.tasks.values():
            for task in rows:
                if task["id"] == task_id:
                    return task
        return None

    def complete_in_app(self, task_id: str) -> None:
        """The person ticks a task off in Google's own app, which hides it."""
        self._apply(self.task(task_id), {"status": "completed"})
        self.task(task_id)["hidden"] = True

    def edit_in_app(self, task_id: str, **changes: Any) -> None:
        self._apply(self.task(task_id), changes)

    def _apply(self, task: Dict[str, Any], changes: Dict[str, Any]) -> None:
        moment = self.stamp()
        for key, value in changes.items():
            if value is None:
                task.pop(key, None)
            else:
                task[key] = value
        if changes.get("status") == "completed" and "completed" not in task:
            task["completed"] = moment
        elif changes.get("status") == "needsAction":
            task.pop("completed", None)
            task.pop("hidden", None)
        task["updated"] = moment

    # -- querying, as Google does ----------------------------------------
    def _select(self, list_id: str, query: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        def flag(name: str, default: bool) -> bool:
            return query.get(name, [str(default).lower()])[0] == "true"

        rows = [t for t in self.tasks[list_id] if not t.get("deleted")]
        if not flag("showCompleted", True):
            rows = [t for t in rows if t["status"] != "completed"]
        if not flag("showHidden", False):
            rows = [t for t in rows if not t.get("hidden")]
        if "updatedMin" in query:
            bound = _moment(query["updatedMin"][0])
            rows = [t for t in rows if _moment(t["updated"]) >= bound]
        if "completedMin" in query:
            bound = _moment(query["completedMin"][0])
            rows = [t for t in rows if t.get("completed") and _moment(t["completed"]) >= bound]
        return rows

    # -- the server ------------------------------------------------------
    def start(self) -> "TasksStub":
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
                    self._json(401, {"error": {"code": 401, "status": "UNAUTHENTICATED",
                                               "message": "Request had invalid authentication credentials."}})
                    return False
                return True

            def _missing(self):
                self._json(404, {"error": {"code": 404, "message": "Requested entity was not found."}})

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
                assert parts[:2] == ["api", "tasks"], url.path
                return parts[2:], urllib.parse.parse_qs(url.query)

            def _list_id(self, name: str) -> str:
                if name == "@default":
                    return next(iter(stub.lists))
                return name

            def _task(self, parts):
                list_id = self._list_id(parts[1])
                for task in stub.tasks.get(list_id, []):
                    if task["id"] == parts[3]:
                        return list_id, task
                return list_id, None

            def _page(self, rows, query):
                top = min(int(query.get("maxResults", ["20"])[0]), PAGE_CAP)
                start = int(query.get("pageToken", ["0"])[0])
                answer: Dict[str, Any] = {"items": rows[start: start + top]}
                if start + top < len(rows):
                    answer["nextPageToken"] = str(start + top)
                return answer

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                parts, query = self._route()
                if parts == ["users", "@me", "lists"]:
                    return self._json(200, self._page(list(stub.lists.values()), query))
                if parts[:3] == ["users", "@me", "lists"] and len(parts) == 4:
                    row = stub.lists.get(self._list_id(parts[3]))
                    return self._json(200, row) if row else self._missing()
                if parts[:1] == ["lists"] and len(parts) == 3 and parts[2] == "tasks":
                    list_id = self._list_id(parts[1])
                    if list_id not in stub.lists:
                        return self._missing()
                    return self._json(200, self._page(stub._select(list_id, query), query))
                if parts[:1] == ["lists"] and len(parts) == 4:
                    _, task = self._task(parts)
                    return self._json(200, task) if task else self._missing()
                self._json(404, {"error": {"code": 404, "message": f"no route {parts}"}})

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                parts, query = self._route()
                body = self._payload()
                if parts == ["users", "@me", "lists"]:
                    stub.writes.append("create list")
                    return self._json(200, stub.lists[stub.add_list(str(body.get("title") or ""))])
                if parts[:1] == ["lists"] and len(parts) == 3 and parts[2] == "tasks":
                    list_id = self._list_id(parts[1])
                    if list_id not in stub.lists:
                        return self._missing()
                    parent = query.get("parent", [""])[0]
                    if parent and stub.task(parent) is None:
                        return self._missing()
                    stub.writes.append("create task")
                    task_id = stub.add_task(list_id, str(body.get("title") or ""),
                                            notes=str(body.get("notes") or ""), parent=parent)
                    task = stub.task(task_id)
                    if body.get("due"):
                        task["due"] = body["due"]
                    return self._json(200, task)
                self._json(404, {"error": {"code": 404, "message": f"no route {parts}"}})

            def do_PATCH(self):  # noqa: N802
                if not self._authed():
                    return
                parts, _ = self._route()
                if len(parts) != 4:
                    return self._missing()
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
                if len(parts) != 4:
                    return self._missing()
                list_id, task = self._task(parts)
                if task is None:
                    return self._missing()
                stub.writes.append(f"delete {task['id']}")
                stub.tasks[list_id] = [t for t in stub.tasks[list_id]
                                       if t["id"] != task["id"] and t.get("parent") != task["id"]]
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
                "api_base_url": self.url + "/api/tasks"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
