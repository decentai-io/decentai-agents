"""A loopback Microsoft Graph: the drive search and workbook subset the
Excel Online and Microsoft Forms agents call, over real HTTP, holding
small fictional workbooks for Sidra Office Supplies.

It behaves the way Graph does where the agents depend on it: a range
answers with values, text and formulas, and an address that names its
worksheet ("'Q3 Orders'!A1:D9"); a used range covers only cells holding
values, and an empty sheet's is a lone A1; an Excel table always keeps
one body row, blank when it holds no data, and the first added row
fills it; rows/add appends at the end; a session is created with
createSession and named in the workbook-session-id header, and every
request carrying one is remembered so a test can see a write rode it.

Cells are held per worksheet as {(row, column): value}. A table is a
box on its worksheet — header row, first and last column, last row —
whose cells are the worksheet's own.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

ITEM = re.compile(r"^/me/drive/items/([^/]+)(/workbook(/.*)?)?$")
RANGE = re.compile(r"^/worksheets/([^/]+)/range\(address='([^']*)'\)$")
USED = re.compile(r"^/worksheets/([^/]+)/usedRange(\(valuesOnly=true\))?$")
TABLE = re.compile(r"^/tables/([^/]+)(/range|/rows/add)?$")


def letters(number: int) -> str:
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


def number(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n


def parse(local: str) -> Tuple[int, int, int, int]:
    cells = []
    for part in local.split(":"):
        match = re.match(r"^\$?([A-Z]+)\$?(\d+)$", part.strip().upper())
        cells.append((int(match.group(2)), number(match.group(1))))
    if len(cells) == 1:
        cells.append(cells[0])
    (ra, ca), (rb, cb) = cells
    return min(ra, rb), min(ca, cb), max(ra, rb), max(ca, cb)


def text_of(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Workbook:
    def __init__(self):
        self.sheets: Dict[str, Dict[Tuple[int, int], Any]] = {}
        # name -> {sheet, header_row, first_col, last_col, last_row}
        self.tables: Dict[str, Dict[str, Any]] = {}

    def qualified(self, sheet: str, local: str) -> str:
        name = sheet if re.match(r"^[A-Za-z0-9_]+$", sheet) else "'" + sheet.replace("'", "''") + "'"
        return f"{name}!{local}"

    def block(self, sheet: str, r1: int, c1: int, r2: int, c2: int) -> Dict[str, Any]:
        grid = self.sheets[sheet]
        values = [[grid.get((r, c), "") for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)]
        local = f"{letters(c1)}{r1}" if (r1, c1) == (r2, c2) else f"{letters(c1)}{r1}:{letters(c2)}{r2}"
        return {"address": self.qualified(sheet, local),
                "rowCount": r2 - r1 + 1, "columnCount": c2 - c1 + 1,
                "values": values, "formulas": values,
                "text": [[text_of(v) for v in line] for line in values]}

    def used(self, sheet: str) -> Dict[str, Any]:
        filled = [k for k, v in self.sheets[sheet].items() if v not in ("", None)]
        if not filled:
            return self.block(sheet, 1, 1, 1, 1)
        rows = [r for r, _ in filled]
        cols = [c for _, c in filled]
        return self.block(sheet, min(rows), min(cols), max(rows), max(cols))

    def table_box(self, name: str) -> Dict[str, Any]:
        t = self.tables[name]
        return self.block(t["sheet"], t["header_row"], t["first_col"], t["last_row"], t["last_col"])

    def body(self, name: str) -> List[List[Any]]:
        t = self.tables[name]
        grid = self.sheets[t["sheet"]]
        return [[grid.get((r, c), "") for c in range(t["first_col"], t["last_col"] + 1)]
                for r in range(t["header_row"] + 1, t["last_row"] + 1)]

    def append(self, name: str, lines: List[List[Any]]) -> int:
        t = self.tables[name]
        grid = self.sheets[t["sheet"]]
        body = self.body(name)
        # The one blank body row of an empty table is filled first.
        start = t["header_row"] + 1 if len(body) == 1 and all(v in ("", None) for v in body[0]) \
            else t["last_row"] + 1
        for i, line in enumerate(lines):
            for j, value in enumerate(line):
                grid[(start + i, t["first_col"] + j)] = value
        t["last_row"] = start + len(lines) - 1
        return start - t["header_row"] - 1

    def remove(self, name: str, index: int) -> None:
        """Delete one data row, the rows below moving up."""
        t = self.tables[name]
        grid = self.sheets[t["sheet"]]
        for r in range(t["header_row"] + 1 + index, t["last_row"] + 1):
            for c in range(t["first_col"], t["last_col"] + 1):
                grid[(r, c)] = grid.get((r + 1, c), "") if r < t["last_row"] else ""
        t["last_row"] -= 1
        if t["last_row"] == t["header_row"]:
            t["last_row"] += 1          # a table keeps its one blank row


class GraphWorkbookStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {}
        self.workbooks: Dict[str, Workbook] = {}
        self.sessions: List[Dict[str, Any]] = []
        self.session_requests: List[Tuple[str, str, str]] = []
        self.writes: List[Tuple[str, str]] = []
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def add_file(self, folder: str, name: str) -> str:
        self._n += 1
        item_id = f"01XLS{self._n:04d}"
        self.items[item_id] = {
            "id": item_id, "name": name, "size": 20480, "file": {"mimeType": "application/octet-stream"},
            "lastModifiedDateTime": "2026-09-01T10:00:00Z",
            "parentReference": {"driveId": "b!demo", "path": "/drive/root:" + (folder if folder != "/" else "")},
            "webUrl": "https://sidra-my.sharepoint.com/personal/demo/Documents" + urllib.parse.quote(
                folder.rstrip("/") + "/" + name)}
        if name.lower().endswith(".xlsx"):
            self.workbooks[item_id] = Workbook()
        return item_id

    def add_sheet(self, item_id: str, sheet: str, rows: List[List[Any]] = (), at: str = "A1") -> None:
        book = self.workbooks[item_id]
        grid = book.sheets.setdefault(sheet, {})
        r0, c0, _, _ = parse(at)
        for i, line in enumerate(rows):
            for j, value in enumerate(line):
                grid[(r0 + i, c0 + j)] = value

    def add_table(self, item_id: str, sheet: str, name: str, headers: List[str],
                  rows: List[List[Any]], at: str = "A1") -> None:
        book = self.workbooks[item_id]
        if sheet not in book.sheets:
            self.add_sheet(item_id, sheet)
        self.add_sheet(item_id, sheet, [headers] + list(rows), at)
        r0, c0, _, _ = parse(at)
        book.tables[name] = {"sheet": sheet, "header_row": r0, "first_col": c0,
                             "last_col": c0 + len(headers) - 1,
                             "last_row": r0 + max(1, len(rows))}

    def append_rows(self, item_id: str, table: str, rows: List[List[Any]]) -> None:
        """Rows a person (or Microsoft Forms) added in Excel."""
        self.workbooks[item_id].append(table, rows)

    def remove_row(self, item_id: str, table: str, index: int) -> None:
        self.workbooks[item_id].remove(table, index)

    def table_rows(self, item_id: str, table: str) -> List[List[Any]]:
        return self.workbooks[item_id].body(table)

    def cell(self, item_id: str, sheet: str, address: str) -> Any:
        r, c, _, _ = parse(address)
        return self.workbooks[item_id].sheets[sheet].get((r, c), "")

    # -- the server ------------------------------------------------------
    def start(self) -> "GraphWorkbookStub":
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

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _payload(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                return (path[4:] if path.startswith("/api") else path), urllib.parse.parse_qs(url.query)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._json(401, {"error": {"code": "InvalidAuthenticationToken",
                                               "message": "Access token has expired."}})
                    return False
                return True

            def _missing(self, what: str = "The resource could not be found."):
                self._json(404, {"error": {"code": "ItemNotFound", "message": what}})

            def _workbook(self, method: str, path: str):
                """(item_id, workbook, the path inside /workbook) — or None
                once an answer has been sent."""
                if not self._authed():
                    return None
                session = self.headers.get("workbook-session-id") or ""
                if session:
                    stub.session_requests.append((method, path, session))
                match = ITEM.match(path)
                if not match:
                    self._missing(f"no route {path}")
                    return None
                item_id, is_workbook, inner = match.groups()
                if item_id not in stub.items:
                    self._missing()
                    return None
                if not is_workbook:
                    return item_id, None, None
                book = stub.workbooks.get(item_id)
                if book is None:
                    self._json(400, {"error": {"code": "BadRequest",
                                               "message": "The file is not a workbook."}})
                    return None
                return item_id, book, inner or ""

            def do_GET(self):  # noqa: N802
                path, query = self._route()
                if path == "/me":
                    if self._authed():
                        self._json(200, {"mail": stub.ACCOUNT, "userPrincipalName": stub.ACCOUNT})
                    return
                match = re.match(r"^/me/drive/root/search\(q='(.*)'\)$", path)
                if match:
                    if not self._authed():
                        return
                    q = match.group(1).replace("''", "'").lower()
                    rows = [i for i in stub.items.values() if q in i["name"].lower()]
                    return self._json(200, {"value": rows[: int(query.get("$top", ["20"])[0])]})
                found = self._workbook("GET", path)
                if found is None:
                    return
                item_id, book, inner = found
                if book is None:
                    return self._json(200, stub.items[item_id])
                if inner == "/worksheets":
                    return self._json(200, {"value": [
                        {"id": f"{{{i:08d}}}", "name": name, "position": i, "visibility": "Visible"}
                        for i, name in enumerate(book.sheets)]})
                if inner == "/tables":
                    return self._json(200, {"value": [
                        {"id": str(i + 1), "name": name, "showHeaders": True, "showTotals": False}
                        for i, name in enumerate(book.tables)]})
                match = USED.match(inner)
                if match:
                    if match.group(1) not in book.sheets:
                        return self._missing()
                    return self._json(200, book.used(match.group(1)))
                match = RANGE.match(inner)
                if match:
                    sheet, local = match.groups()
                    if sheet not in book.sheets:
                        return self._missing()
                    return self._json(200, book.block(sheet, *parse(local)))
                match = TABLE.match(inner)
                if match:
                    name, tail = match.groups()
                    if name not in book.tables:
                        return self._missing()
                    if tail == "/range":
                        return self._json(200, book.table_box(name))
                    if not tail:
                        return self._json(200, {"id": name, "name": name,
                                                "showHeaders": True, "showTotals": False})
                self._missing(f"no route {path}")

            def do_POST(self):  # noqa: N802
                path, _ = self._route()
                found = self._workbook("POST", path)
                if found is None:
                    return
                item_id, book, inner = found
                body = self._payload()
                if inner == "/createSession":
                    session = {"id": f"session-{len(stub.sessions) + 1}",
                               "persistChanges": bool(body.get("persistChanges")), "closed": False}
                    stub.sessions.append(session)
                    return self._json(201, {"id": session["id"],
                                            "persistChanges": session["persistChanges"]})
                if inner == "/closeSession":
                    named = self.headers.get("workbook-session-id")
                    for session in stub.sessions:
                        if session["id"] == named:
                            session["closed"] = True
                    return self._empty(204)
                match = TABLE.match(inner or "")
                if match and match.group(2) == "/rows/add":
                    name = match.group(1)
                    if name not in book.tables:
                        return self._missing()
                    t = book.tables[name]
                    width = t["last_col"] - t["first_col"] + 1
                    if any(len(line) != width for line in body.get("values") or []):
                        return self._json(400, {"error": {
                            "code": "InvalidArgument",
                            "message": "The number of columns does not match the table."}})
                    stub.writes.append(("rows/add", name))
                    index = book.append(name, body["values"])
                    return self._json(201, {"index": index, "values": body["values"]})
                self._missing(f"no route {path}")

            def do_PATCH(self):  # noqa: N802
                path, _ = self._route()
                found = self._workbook("PATCH", path)
                if found is None:
                    return
                item_id, book, inner = found
                match = RANGE.match(inner or "")
                if not match or match.group(1) not in book.sheets:
                    return self._missing()
                sheet, local = match.groups()
                r1, c1, r2, c2 = parse(local)
                values = self._payload().get("values") or []
                if len(values) != r2 - r1 + 1 or any(len(v) != c2 - c1 + 1 for v in values):
                    return self._json(400, {"error": {
                        "code": "InvalidArgument",
                        "message": "The number of rows or columns in the input array "
                                   "doesn't match the size or dimensions of the range."}})
                grid = book.sheets[sheet]
                for i, line in enumerate(values):
                    for j, value in enumerate(line):
                        if value is not None:
                            grid[(r1 + i, c1 + j)] = value
                stub.writes.append(("range", f"{sheet}!{local}"))
                self._json(200, book.block(sheet, r1, c1, r2, c2))

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
