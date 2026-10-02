"""A loopback Todoist: the subset of the unified API v1 the Todoist agent
calls, over real HTTP, holding a fictional task list for Sidra Office
Supplies. List endpoints answer v1's envelope — ``results`` and
``next_cursor``, and ``items`` for completed tasks — and page by cursor;
every call wants the bearer token. The completed range is inclusive at
both ends and refused past three months, as Todoist documents it, and
answered newest first so the agent's own ordering is what is tested.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

TOKEN = "tk-1"


def _moment(text: str) -> datetime:
    text = str(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    """Todoist's own spelling of a moment."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TodoistStub:
    ACCOUNT = "sam@sidra.example"
    TIMEZONE = "Asia/Dubai"

    def __init__(self):
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.sections: Dict[str, Dict[str, Any]] = {}
        self.labels: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.comments: Dict[str, Dict[str, Any]] = {}
        self.writes: List[tuple] = []            # (method, path, body) of every write
        self.completed_queries: List[Dict[str, str]] = []
        self._n = 1000
        self._server: Optional[ThreadingHTTPServer] = None

    # -- seeding ---------------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return f"6X{self._n}"

    def add_project(self, name: str, shared: bool = False, inbox: bool = False,
                    parent_id: Optional[str] = None) -> str:
        project_id = self._id()
        self.projects[project_id] = {
            "id": project_id, "name": name, "parent_id": parent_id,
            "is_shared": shared, "inbox_project": inbox, "is_archived": False,
            "is_deleted": False, "color": "charcoal", "child_order": len(self.projects),
        }
        return project_id

    def add_section(self, project_id: str, name: str) -> str:
        section_id = self._id()
        self.sections[section_id] = {"id": section_id, "project_id": project_id,
                                     "name": name, "is_archived": False}
        return section_id

    def add_label(self, name: str) -> str:
        label_id = self._id()
        self.labels[label_id] = {"id": label_id, "name": name, "color": "blue"}
        return label_id

    def add_task(self, content: str, project_id: str, *, section_id: Optional[str] = None,
                 labels=None, priority: int = 1, due_date: Optional[str] = None,
                 due_string: Optional[str] = None, recurring: bool = False,
                 description: str = "") -> str:
        task_id = self._id()
        due = None
        if due_date:
            due = {"date": due_date, "string": due_string or due_date,
                   "is_recurring": recurring, "lang": "en", "timezone": None}
        self.tasks[task_id] = {
            "id": task_id, "user_id": "u1", "project_id": project_id,
            "section_id": section_id, "parent_id": None, "labels": list(labels or []),
            "priority": priority, "due": due, "content": content,
            "description": description, "checked": False, "is_deleted": False,
            "added_at": "2026-09-01T08:00:00.000000Z", "completed_at": None,
            "child_order": len(self.tasks),
        }
        return task_id

    def complete_at(self, task_id: str, moment: datetime) -> None:
        """A task ticked off in the Todoist app at that moment."""
        task = self.tasks[task_id]
        task["checked"] = True
        task["completed_at"] = stamp(moment)

    # -- server ----------------------------------------------------------
    def start(self) -> "TodoistStub":
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
                self.end_headers()

            def _error(self, status: int, message: str) -> None:
                self._json(status, {"error": message, "error_code": status,
                                    "http_code": status, "error_extra": {}})

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw or b"{}")

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._error(401, "Unauthorized")
                    return False
                return True

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = url.path
                assert path.startswith("/api/v1/"), path
                return path[len("/api/v1/"):], {
                    k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}

            def _page(self, rows, query, key="results"):
                limit = int(query.get("limit") or 50)
                if not 1 <= limit <= 200:
                    return self._error(400, "limit must be 1..200")
                offset = int(query.get("cursor") or 0)
                page = rows[offset: offset + limit]
                more = offset + limit < len(rows)
                self._json(200, {key: page,
                                 "next_cursor": str(offset + limit) if more else None})

            def _open_task(self, task_id):
                task = stub.tasks.get(task_id)
                if task is None or task["is_deleted"]:
                    self._error(404, "Task not found")
                    return None
                return task

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                path, query = self._route()
                if path == "user":
                    return self._json(200, {"id": "u1", "email": stub.ACCOUNT,
                                            "full_name": "Sam Haddad",
                                            "tz_info": {"timezone": stub.TIMEZONE,
                                                        "gmt_string": "+04:00", "hours": 4,
                                                        "minutes": 0, "is_dst": 0}})
                if path == "projects":
                    return self._page(list(stub.projects.values()), query)
                if path == "sections":
                    rows = [s for s in stub.sections.values()
                            if not query.get("project_id")
                            or s["project_id"] == query["project_id"]]
                    return self._page(rows, query)
                if path == "labels":
                    return self._page(list(stub.labels.values()), query)
                if path == "tasks":
                    rows = [t for t in stub.tasks.values()
                            if not t["checked"] and not t["is_deleted"]
                            and (not query.get("project_id") or t["project_id"] == query["project_id"])
                            and (not query.get("section_id") or t["section_id"] == query["section_id"])
                            and (not query.get("label") or query["label"] in t["labels"])]
                    return self._page(rows, query)
                if path == "tasks/filter":
                    matcher = stub._filter(query.get("query") or "")
                    if matcher is None:
                        return self._error(400, "Invalid filter query")
                    rows = [t for t in stub.tasks.values()
                            if not t["checked"] and not t["is_deleted"] and matcher(t)]
                    return self._page(rows, query)
                if path == "tasks/completed/by_completion_date":
                    stub.completed_queries.append(dict(query))
                    try:
                        since, until = _moment(query["since"]), _moment(query["until"])
                    except (KeyError, ValueError):
                        return self._error(400, "since and until are required date-times")
                    if until < since or until - since > timedelta(days=92):
                        return self._error(400, "The range must be at most 3 months")
                    rows = [t for t in stub.tasks.values()
                            if t["checked"] and not t["is_deleted"]
                            and since <= _moment(t["completed_at"]) <= until
                            and (not query.get("project_id")
                                 or t["project_id"] == query["project_id"])]
                    rows.sort(key=lambda t: t["completed_at"], reverse=True)
                    return self._page(rows, query, key="items")
                if path.startswith("tasks/"):
                    task = self._open_task(path.split("/")[1])
                    if task is not None:
                        self._json(200, task)
                    return
                if path == "comments":
                    rows = [c for c in stub.comments.values()
                            if c["task_id"] == query.get("task_id")]
                    return self._page(rows, query)
                self._error(404, f"no route {path}")

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                path, _ = self._route()
                body = self._payload()
                stub.writes.append(("POST", path, body))
                if path == "tasks":
                    return stub._create_task(self, body)
                if path == "projects":
                    project_id = stub.add_project(body["name"], parent_id=body.get("parent_id"))
                    return self._json(200, stub.projects[project_id])
                if path == "comments":
                    if self._open_task(body.get("task_id")) is None:
                        return
                    comment_id = stub._id()
                    stub.comments[comment_id] = {
                        "id": comment_id, "task_id": body["task_id"],
                        "content": body["content"],
                        "posted_at": "2026-09-13T08:00:00.000000Z"}
                    return self._json(200, stub.comments[comment_id])
                parts = path.split("/")
                if len(parts) >= 2 and parts[0] == "tasks":
                    task = self._open_task(parts[1])
                    if task is None:
                        return
                    if len(parts) == 3 and parts[2] == "close":
                        stub.complete_at(task["id"], datetime.now(timezone.utc))
                        return self._empty()
                    if len(parts) == 3 and parts[2] == "reopen":
                        task["checked"], task["completed_at"] = False, None
                        return self._empty()
                    if len(parts) == 2:
                        return stub._update_task(self, task, body)
                self._error(404, f"no route {path}")

            def do_DELETE(self):  # noqa: N802
                if not self._authed():
                    return
                path, _ = self._route()
                stub.writes.append(("DELETE", path, {}))
                task = self._open_task(path.split("/")[-1])
                if task is None:
                    return
                task["is_deleted"] = True
                self._empty()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    # -- behaviour -------------------------------------------------------
    def _due(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if body.get("due_date"):
            return {"date": body["due_date"], "string": body["due_date"],
                    "is_recurring": False, "lang": "en", "timezone": None}
        words = str(body.get("due_string") or "")
        if words == "no date":
            return None
        today = date.today()
        when = {"today": today, "tomorrow": today + timedelta(days=1)}.get(words, today)
        return {"date": when.isoformat(), "string": words,
                "is_recurring": words.startswith("every"), "lang": "en", "timezone": None}

    def _check_labels(self, handler, labels) -> bool:
        # Real Todoist would create an unknown personal label silently;
        # the stub refuses so a test notices the agent let one through.
        known = {l["name"] for l in self.labels.values()}
        if any(name not in known for name in labels or []):
            handler._error(400, "stub: unknown label sent")
            return False
        return True

    def _create_task(self, handler, body):
        project_id = body.get("project_id") or next(
            p["id"] for p in self.projects.values() if p["inbox_project"])
        if project_id not in self.projects:
            return handler._error(400, "Invalid argument value: project_id")
        if not self._check_labels(handler, body.get("labels")):
            return
        task_id = self.add_task(body["content"], project_id,
                                section_id=body.get("section_id"),
                                labels=body.get("labels"), priority=body.get("priority", 1),
                                description=body.get("description", ""))
        if body.get("due_string") or body.get("due_date"):
            self.tasks[task_id]["due"] = self._due(body)
        handler._json(200, self.tasks[task_id])

    def _update_task(self, handler, task, body):
        if not self._check_labels(handler, body.get("labels")):
            return
        for field in ("content", "description", "priority", "labels"):
            if field in body:
                task[field] = body[field]
        if "due_string" in body or "due_date" in body:
            task["due"] = self._due(body)
        handler._json(200, task)

    def _filter(self, query: str):
        """A sliver of Todoist's filter language: today, overdue, #Project,
        @label and p1–p4, joined by '|' (or) and '&' (and)."""
        today = date.today().isoformat()

        def term(word):
            word = word.strip()
            if word == "today":
                return lambda t: bool(t["due"]) and t["due"]["date"][:10] == today
            if word == "overdue":
                return lambda t: bool(t["due"]) and t["due"]["date"][:10] < today
            if word.startswith("#"):
                ids = {p["id"] for p in self.projects.values()
                       if p["name"].lower() == word[1:].lower()}
                return lambda t: t["project_id"] in ids
            if word.startswith("@"):
                return lambda t: word[1:] in t["labels"]
            if word in ("p1", "p2", "p3", "p4"):
                return lambda t: t["priority"] == 5 - int(word[1])
            return None

        alternatives = []
        for alternative in query.split("|"):
            terms = [term(w) for w in alternative.split("&")]
            if not terms or any(t is None for t in terms):
                return None
            alternatives.append(terms)
        return lambda task: any(all(t(task) for t in terms) for terms in alternatives)

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, access_token: str = TOKEN) -> Dict[str, Any]:
        """The credential as the platform hands it to the agent: the
        account, the token, and the loopback base URL."""
        return {"account": self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url + "/api/v1"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
