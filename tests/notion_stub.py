"""A loopback Notion API (version 2022-06-28): the subset the Notion
agent calls, over real HTTP, holding a small fictional workspace for
Sidra Office Supplies.

It behaves the way Notion does where the agent depends on it: every
request must carry the pinned Notion-Version and the bearer token;
last_edited_time is kept to the minute (the stub's ``clock``, which a
test moves); a page the integration was not given answers 403
restricted_resource and an unknown id 404 object_not_found; lists page
with start_cursor / next_cursor / has_more; a database query takes the
timestamp filter on last_edited_time and simple property filters; and
a property the database does not define is refused as a validation
error.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

VERSION = "2022-06-28"
TOKEN = "at-1"


def rich(text: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": {"content": text, "link": None},
             "annotations": {"bold": False}, "plain_text": text, "href": None}]


def plain(parts: Any) -> str:
    return "".join(str(p.get("plain_text") or (p.get("text") or {}).get("content") or "")
                   for p in parts or [])


class NotionStub:
    ACCOUNT = "demo@sidra.example"
    WORKSPACE = "Sidra Office Supplies"

    def __init__(self):
        self.clock = "2026-09-10T09:00:00.000Z"
        self.databases: Dict[str, Dict[str, Any]] = {}
        self.pages: Dict[str, Dict[str, Any]] = {}
        self.children: Dict[str, List[Dict[str, Any]]] = {}
        self.comments: List[Dict[str, Any]] = []
        self.restricted: set = set()
        self.created: List[Dict[str, Any]] = []
        self.updated: List[Dict[str, Any]] = []
        self.appended: List[Dict[str, Any]] = []
        self.commented: List[Dict[str, Any]] = []
        self.queries: List[Dict[str, Any]] = []
        self.requests = 0
        self.unversioned = 0
        self.limit_next = 0
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def _id(self) -> str:
        self._n += 1
        return f"{self._n:08x}-5eed-4a11-8b0c-{self._n:012x}"

    @staticmethod
    def _url(title: str, page_id: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
        return f"https://www.notion.so/{slug}-{page_id.replace('-', '')}"

    def add_database(self, title: str, schema: Dict[str, Any], parent_page: str = "") -> str:
        """schema: name -> type, or name -> (type, [options])."""
        database_id = self._id()
        properties = {}
        for name, spec in schema.items():
            kind, options = (spec, []) if isinstance(spec, str) else spec
            prop: Dict[str, Any] = {"id": name[:4].lower(), "name": name, "type": kind, kind: {}}
            if options:
                prop[kind] = {"options": [{"id": f"o{i}", "name": o, "color": "default"}
                                          for i, o in enumerate(options)]}
            properties[name] = prop
        self.databases[database_id] = {
            "object": "database", "id": database_id, "title": rich(title),
            "url": self._url(title, database_id), "created_time": self.clock,
            "last_edited_time": self.clock, "properties": properties,
            "parent": ({"type": "page_id", "page_id": parent_page} if parent_page
                       else {"type": "workspace", "workspace": True}),
            "archived": False}
        return database_id

    def _value(self, database_id: str, name: str, value: Any) -> Dict[str, Any]:
        """A Python value as the property object Notion returns."""
        prop = self.databases[database_id]["properties"][name]
        kind = prop["type"]
        out: Dict[str, Any] = {"id": prop["id"], "type": kind}
        if kind in ("title", "rich_text"):
            out[kind] = rich(value) if value else []
        elif kind in ("select", "status"):
            out[kind] = {"name": value, "color": "default"} if value else None
        elif kind == "multi_select":
            out[kind] = [{"name": v, "color": "default"} for v in value or []]
        elif kind == "date":
            out[kind] = {"start": value, "end": None} if value else None
        else:
            out[kind] = value
        return out

    def _output(self, database_id: str, name: str, given: Dict[str, Any]) -> Dict[str, Any]:
        """A property as the agent sent it, as Notion stores and returns it."""
        prop = self.databases[database_id]["properties"][name]
        kind = prop["type"]
        value = given.get(kind)
        if kind in ("title", "rich_text"):
            value = rich(plain(value)) if plain(value) else []
        elif kind in ("select", "status") and value:
            value = {"name": value["name"], "color": "default"}
        elif kind == "multi_select":
            value = [{"name": v["name"], "color": "default"} for v in value or []]
        return {"id": prop["id"], "type": kind, kind: value}

    def add_row(self, database_id: str, values: Dict[str, Any],
                created: str = "", edited: str = "") -> str:
        page_id = self._id()
        database = self.databases[database_id]
        title_name = next(n for n, p in database["properties"].items() if p["type"] == "title")
        properties = {name: self._value(database_id, name, values.get(name))
                      for name in database["properties"]}
        self.pages[page_id] = {
            "object": "page", "id": page_id, "created_time": created or self.clock,
            "last_edited_time": edited or created or self.clock,
            "created_by": {"object": "user", "id": "user-dana"},
            "parent": {"type": "database_id", "database_id": database_id},
            "archived": False, "in_trash": False, "properties": properties,
            "url": self._url(str(values.get(title_name) or ""), page_id)}
        return page_id

    def edit_row(self, page_id: str, values: Dict[str, Any]) -> None:
        """A person editing a row in Notion, at the stub's clock."""
        page = self.pages[page_id]
        database_id = page["parent"]["database_id"]
        for name, value in values.items():
            page["properties"][name] = self._value(database_id, name, value)
        page["last_edited_time"] = self.clock

    def add_page(self, title: str, parent_page: str = "", edited: str = "") -> str:
        page_id = self._id()
        self.pages[page_id] = {
            "object": "page", "id": page_id, "created_time": edited or self.clock,
            "last_edited_time": edited or self.clock,
            "parent": ({"type": "page_id", "page_id": parent_page} if parent_page
                       else {"type": "workspace", "workspace": True}),
            "archived": False, "in_trash": False,
            "properties": {"title": {"id": "title", "type": "title", "title": rich(title)}},
            "url": self._url(title, page_id)}
        if parent_page:
            self.add_block(parent_page, "child_page", title=title, block_id=page_id)
        return page_id

    def add_block(self, parent: str, kind: str, text: str = "", block_id: str = "",
                  **extra) -> str:
        block_id = block_id or self._id()
        if kind in ("child_page", "child_database"):
            body: Dict[str, Any] = {"title": extra.pop("title", text)}
        elif kind == "divider":
            body = {}
        else:
            body = {"rich_text": rich(text), "color": "default"}
        body.update(extra)
        self.children.setdefault(parent, []).append({
            "object": "block", "id": block_id, "type": kind, kind: body,
            "has_children": False, "archived": False,
            "created_time": self.clock, "last_edited_time": self.clock})
        # A block that gains a child says so, as Notion's has_children does.
        for siblings in self.children.values():
            for block in siblings:
                if block["id"] == parent:
                    block["has_children"] = True
        return block_id

    def add_comment(self, page_id: str, text: str, author: str = "user-dana") -> str:
        comment_id = self._id()
        self.comments.append({"object": "comment", "id": comment_id,
                              "parent": {"type": "page_id", "page_id": page_id},
                              "discussion_id": "disc-" + comment_id[:8],
                              "created_time": self.clock, "last_edited_time": self.clock,
                              "created_by": {"object": "user", "id": author},
                              "rich_text": rich(text)})
        return comment_id

    # -- queries -------------------------------------------------------
    @staticmethod
    def _text(prop: Dict[str, Any]) -> str:
        kind = prop["type"]
        value = prop.get(kind)
        if kind in ("title", "rich_text"):
            return plain(value)
        if kind in ("select", "status"):
            return (value or {}).get("name") or ""
        if kind == "date":
            return (value or {}).get("start") or ""
        return "" if value is None else str(value)

    def _matches(self, page: Dict[str, Any], filter_: Optional[Dict[str, Any]]) -> bool:
        if not filter_:
            return True
        if filter_.get("timestamp"):
            kind = filter_["timestamp"]
            condition = filter_[kind]
            return page[kind] >= condition["on_or_after"]
        prop = page["properties"][filter_["property"]]
        kind = prop["type"]
        condition = filter_[kind]
        (operator, wanted), = condition.items()
        value = prop.get(kind)
        if kind == "multi_select":
            return any(o["name"] == wanted for o in value or [])
        if kind == "checkbox":
            return bool(value) == wanted
        if kind == "number":
            return value == wanted
        text = self._text(prop)
        if operator == "equals":
            return text == wanted
        if operator == "contains":
            return wanted.lower() in text.lower()
        if operator == "on_or_after":
            return bool(text) and text >= wanted
        raise AssertionError(f"stub cannot filter {filter_}")

    def _sorted(self, pages: List[Dict[str, Any]], sorts: List[Dict[str, Any]]):
        for sort in reversed(sorts or []):
            if sort.get("timestamp"):
                key = (lambda p, k=sort["timestamp"]: p[k])
            else:
                key = (lambda p, n=sort["property"]: self._text(p["properties"][n]))
            pages = sorted(pages, key=key, reverse=sort["direction"] == "descending")
        return pages

    @staticmethod
    def _page_of(items: List[Dict[str, Any]], body: Dict[str, Any]) -> Dict[str, Any]:
        start = int(body.get("start_cursor") or 0)
        size = int(body.get("page_size") or 100)
        chunk = items[start:start + size]
        more = start + size < len(items)
        return {"object": "list", "results": chunk, "has_more": more,
                "next_cursor": str(start + size) if more else None}

    # -- the server ----------------------------------------------------
    def start(self) -> "NotionStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, status: int, code: str, message: str):
                self._json(status, {"object": "error", "status": status,
                                    "code": code, "message": message})

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _gate(self) -> Optional[tuple]:
                """Version, token, rate limit; then the route."""
                stub.requests += 1
                if self.headers.get("Notion-Version") != VERSION:
                    stub.unversioned += 1
                    self._error(400, "missing_version",
                                "Notion-Version header failed validation.")
                    return None
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._error(401, "unauthorized", "API token is invalid.")
                    return None
                if stub.limit_next:
                    stub.limit_next -= 1
                    self._json(429, {"object": "error", "status": 429, "code": "rate_limited",
                                     "message": "You have been rate limited."},
                               {"Retry-After": "7"})
                    return None
                url = urllib.parse.urlparse(self.path)
                path = url.path[len("/v1"):] if url.path.startswith("/v1") else url.path
                return path.strip("/").split("/"), urllib.parse.parse_qs(url.query)

            def _object(self, kind: str, object_id: str) -> Optional[Dict[str, Any]]:
                if object_id in stub.restricted:
                    self._error(403, "restricted_resource",
                                "Insufficient permissions for this endpoint.")
                    return None
                found = (stub.pages if kind == "page" else stub.databases).get(object_id)
                if found is None:
                    self._error(404, "object_not_found",
                                f"Could not find {kind} with ID: {object_id}. Make sure the "
                                f"relevant pages and databases are shared with your integration.")
                return found

            def do_GET(self):  # noqa: N802
                routed = self._gate()
                if routed is None:
                    return
                parts, query = routed
                if parts == ["users", "me"]:
                    return self._json(200, {
                        "object": "user", "id": "bot-sidra", "type": "bot",
                        "name": "DecentAI", "bot": {"owner": {"type": "workspace", "workspace": True},
                                                    "workspace_name": stub.WORKSPACE}})
                if len(parts) == 2 and parts[0] == "pages":
                    page = self._object("page", parts[1])
                    return page is not None and self._json(200, page)
                if len(parts) == 2 and parts[0] == "databases":
                    database = self._object("database", parts[1])
                    return database is not None and self._json(200, database)
                if len(parts) == 3 and parts[0] == "blocks" and parts[2] == "children":
                    parent = parts[1]
                    if parent in stub.restricted:
                        return self._error(403, "restricted_resource", "Insufficient permissions.")
                    known = parent in stub.pages or any(
                        b["id"] == parent for bs in stub.children.values() for b in bs)
                    if not known:
                        return self._error(404, "object_not_found", f"Could not find block {parent}.")
                    body = {k: v[0] for k, v in query.items()}
                    return self._json(200, stub._page_of(stub.children.get(parent, []), body))
                if parts == ["comments"]:
                    block_id = query.get("block_id", [""])[0]
                    if self._object("page", block_id) is None:
                        return
                    rows = [c for c in stub.comments if c["parent"]["page_id"] == block_id]
                    return self._json(200, stub._page_of(rows, {k: v[0] for k, v in query.items()}))
                self._error(400, "invalid_request_url", f"no route GET {parts}")

            def do_POST(self):  # noqa: N802
                routed = self._gate()
                if routed is None:
                    return
                parts, _ = routed
                body = self._payload()
                if parts == ["search"]:
                    wanted = str(body.get("query") or "").lower()
                    kind = (body.get("filter") or {}).get("value")
                    items = [o for o in list(stub.pages.values()) + list(stub.databases.values())
                             if o["id"] not in stub.restricted
                             and (not kind or o["object"] == kind)
                             and wanted in self._title(o).lower()]
                    items.sort(key=lambda o: o["last_edited_time"], reverse=True)
                    return self._json(200, stub._page_of(items, body))
                if len(parts) == 3 and parts[0] == "databases" and parts[2] == "query":
                    database = self._object("database", parts[1])
                    if database is None:
                        return
                    stub.queries.append(body)
                    rows = [p for p in stub.pages.values()
                            if p["parent"].get("database_id") == parts[1]
                            and stub._matches(p, body.get("filter"))]
                    rows = stub._sorted(rows, body.get("sorts"))
                    return self._json(200, stub._page_of(rows, body))
                if parts == ["pages"]:
                    return self._create(body)
                if parts == ["comments"]:
                    page_id = body["parent"]["page_id"]
                    if self._object("page", page_id) is None:
                        return
                    stub.commented.append(body)
                    comment_id = stub.add_comment(page_id, plain(body["rich_text"]), "bot-sidra")
                    return self._json(200, next(c for c in stub.comments if c["id"] == comment_id))
                self._error(400, "invalid_request_url", f"no route POST {parts}")

            @staticmethod
            def _title(obj: Dict[str, Any]) -> str:
                if obj["object"] == "database":
                    return plain(obj["title"])
                return next((plain(p["title"]) for p in obj["properties"].values()
                             if p["type"] == "title"), "")

            def _create(self, body: Dict[str, Any]):
                parent = body.get("parent") or {}
                properties = body.get("properties") or {}
                if parent.get("database_id"):
                    database = self._object("database", parent["database_id"])
                    if database is None:
                        return
                    unknown = [n for n in properties if n not in database["properties"]]
                    if unknown:
                        return self._error(400, "validation_error",
                                           f"{unknown[0]} is not a property that exists.")
                    page_id = stub.add_row(parent["database_id"], {})
                    page = stub.pages[page_id]
                    for name, given in properties.items():
                        page["properties"][name] = stub._output(parent["database_id"], name, given)
                else:
                    if self._object("page", parent.get("page_id", "")) is None:
                        return
                    page_id = stub.add_page(plain(properties["title"]["title"]), parent["page_id"])
                    page = stub.pages[page_id]
                page["url"] = stub._url(self._title(page), page_id)
                for child in body.get("children") or []:
                    stub.add_block(page_id, child["type"], plain(child[child["type"]]["rich_text"]))
                stub.created.append(body)
                self._json(200, page)

            def do_PATCH(self):  # noqa: N802
                routed = self._gate()
                if routed is None:
                    return
                parts, _ = routed
                body = self._payload()
                if len(parts) == 2 and parts[0] == "pages":
                    page = self._object("page", parts[1])
                    if page is None:
                        return
                    database_id = page["parent"].get("database_id")
                    for name, given in (body.get("properties") or {}).items():
                        if database_id:
                            if name not in stub.databases[database_id]["properties"]:
                                return self._error(400, "validation_error",
                                                   f"{name} is not a property that exists.")
                            page["properties"][name] = stub._output(database_id, name, given)
                        else:
                            page["properties"]["title"]["title"] = rich(plain(given["title"]))
                    page["last_edited_time"] = stub.clock
                    stub.updated.append({"page_id": parts[1], **body})
                    return self._json(200, page)
                if len(parts) == 3 and parts[0] == "blocks" and parts[2] == "children":
                    page = self._object("page", parts[1])
                    if page is None:
                        return
                    made = []
                    for child in body.get("children") or []:
                        kind = child["type"]
                        extra = {"checked": child[kind]["checked"]} if kind == "to_do" else {}
                        made.append(stub.add_block(parts[1], kind,
                                                   plain(child[kind]["rich_text"]), **extra))
                    page["last_edited_time"] = stub.clock
                    stub.appended.append({"page_id": parts[1], **body})
                    return self._json(200, {"object": "list", "has_more": False, "next_cursor": None,
                                            "results": [b for b in stub.children[parts[1]]
                                                        if b["id"] in made]})
                self._error(400, "invalid_request_url", f"no route PATCH {parts}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, access_token: str = TOKEN) -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url + "/v1"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
