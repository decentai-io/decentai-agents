"""A loopback Google Sheets: the v4 subset the Google Sheets agent calls,
plus the Drive files list it finds spreadsheets with, over real HTTP,
holding a few fictional spreadsheets for Sidra Office Supplies.

It behaves the way Sheets does where the agent depends on it: a tab is
named inside an A1 range, quoted when it must be; a range past the tab's
grid is refused ("exceeds grid limits"); values come back as the text a
person sees, with trailing empty cells and rows left out and an empty
row in the middle kept as an empty list; an append goes after the last
row of the table and, with INSERT_ROWS, grows the grid; an update whose
values overflow an explicit range is refused.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

SPREADSHEET = "application/vnd.google-apps.spreadsheet"
CELLS = re.compile(r"^([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?$")


def letter(number: int) -> str:
    out = ""
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


def number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


class RangeError(Exception):
    pass


class GoogleSheetsStub:
    ACCOUNT = "demo@sidra.example"

    def __init__(self):
        self.spreadsheets: Dict[str, Dict[str, Any]] = {}
        self.writes: List[str] = []
        self.drop_writes = False
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def add_spreadsheet(self, title: str, tabs: Dict[str, List[List[str]]],
                        modified: str = "2026-09-01T10:00:00.000Z",
                        grid_rows: int = 1000, grid_columns: int = 26) -> str:
        self._n += 1
        spreadsheet_id = f"1sHt{self._n:04d}Sidra"
        self.spreadsheets[spreadsheet_id] = {
            "id": spreadsheet_id, "title": title, "modified": modified,
            "tabs": [{"sheet_id": 100 + index, "title": name, "grid_rows": grid_rows,
                      "grid_columns": grid_columns, "cells": [list(r) for r in rows]}
                     for index, (name, rows) in enumerate(tabs.items())]}
        return spreadsheet_id

    def tab(self, spreadsheet_id: str, title: str) -> Dict[str, Any]:
        return next(t for t in self.spreadsheets[spreadsheet_id]["tabs"] if t["title"] == title)

    def add_rows(self, spreadsheet_id: str, title: str, rows: List[List[str]]) -> None:
        """Someone else adds rows at the end — a form response, a sign-up."""
        tab = self.tab(spreadsheet_id, title)
        at = self._last_row(tab)
        tab["cells"][at:at] = [list(r) for r in rows]
        tab["grid_rows"] = max(tab["grid_rows"], len(tab["cells"]))

    def delete_rows(self, spreadsheet_id: str, title: str, first: int, count: int) -> None:
        """Rows removed (1-based), the way deleting them in the UI shrinks the grid."""
        tab = self.tab(spreadsheet_id, title)
        del tab["cells"][first - 1:first - 1 + count]
        tab["grid_rows"] -= count

    @staticmethod
    def _last_row(tab: Dict[str, Any]) -> int:
        last = 0
        for index, row in enumerate(tab["cells"]):
            if any(str(c) for c in row):
                last = index + 1
        return last

    # -- A1 --------------------------------------------------------------
    def _parse(self, spreadsheet: Dict[str, Any], text: str) -> Tuple[Dict[str, Any], int, int, int, int, bool]:
        """(tab, top, left, bottom, right, explicit end)."""
        if text.startswith("'"):
            index, name = 1, ""
            while index < len(text):
                if text[index] == "'":
                    if text[index + 1:index + 2] == "'":
                        name += "'"
                        index += 2
                        continue
                    break
                name += text[index]
                index += 1
            rest = text[index + 1:]
        else:
            name, _, rest = text.partition("!")
            rest = "!" + rest if rest else ""
        cells = rest[1:] if rest.startswith("!") else ""
        tab = next((t for t in spreadsheet["tabs"] if t["title"] == name), None)
        match = CELLS.match(cells)
        if tab is None or match is None:
            raise RangeError(f"Unable to parse range: {text}")
        c1, r1, c2, r2 = match.groups()
        has_end = ":" in cells
        left = number(c1) if c1 else 1
        top = int(r1) if r1 else 1
        if has_end:
            right = number(c2) if c2 else tab["grid_columns"]
            bottom = int(r2) if r2 else tab["grid_rows"]
        elif cells:
            right, bottom = left, top
        else:
            right, bottom = tab["grid_columns"], tab["grid_rows"]
        if bottom > tab["grid_rows"] or right > tab["grid_columns"]:
            raise RangeError(f"Range ({text}) exceeds grid limits. Max rows: "
                             f"{tab['grid_rows']}, max columns: {tab['grid_columns']}")
        return tab, top, left, bottom, right, has_end or not cells

    @staticmethod
    def _name(tab: Dict[str, Any]) -> str:
        return "'" + tab["title"].replace("'", "''") + "'"

    def _read(self, spreadsheet: Dict[str, Any], text: str) -> Dict[str, Any]:
        tab, top, left, bottom, right, _ = self._parse(spreadsheet, text)
        rows = []
        for r in range(top, bottom + 1):
            source = tab["cells"][r - 1] if r - 1 < len(tab["cells"]) else []
            row = [str(c) for c in source[left - 1:right]]
            while row and row[-1] == "":
                row.pop()
            rows.append(row)
        while rows and not rows[-1]:
            rows.pop()
        answer = {"range": f"{self._name(tab)}!{letter(left)}{top}:{letter(right)}{bottom}",
                  "majorDimension": "ROWS"}
        if rows:
            answer["values"] = rows
        return answer

    # -- the server ------------------------------------------------------
    def start(self) -> "GoogleSheetsStub":
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

            def _error(self, status: int, message: str) -> None:
                self._json(status, {"error": {"code": status, "message": message}})

            def _route(self):
                url = urllib.parse.urlparse(self.path)
                raw = url.path[4:] if url.path.startswith("/api") else url.path
                segments = [urllib.parse.unquote(s) for s in raw.split("/")]
                return segments, urllib.parse.parse_qs(url.query)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != "Bearer at-1":
                    self._error(401, "Request had invalid authentication credentials.")
                    return False
                return True

            def _body(self) -> Dict[str, Any]:
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

            def _spreadsheet(self, segments) -> Optional[Dict[str, Any]]:
                if len(segments) < 4 or segments[1:3] != ["v4", "spreadsheets"]:
                    return None
                return stub.spreadsheets.get(segments[3])

            def _drop(self) -> bool:
                if stub.drop_writes:
                    # The write was received; no answer ever comes.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return True
                return False

            def do_GET(self):  # noqa: N802
                segments, query = self._route()
                if not self._authed():
                    return
                path = "/".join(segments)
                if path == "/drive/v3/about":
                    return self._json(200, {"user": {"emailAddress": stub.ACCOUNT}})
                if path == "/drive/v3/files":
                    q = query.get("q", [""])[0]
                    wanted = re.search(r"name contains '((?:[^'\\]|\\.)*)'", q)
                    rows = [s for s in stub.spreadsheets.values()
                            if f"mimeType = '{SPREADSHEET}'" in q
                            and (not wanted or re.sub(r"\\(.)", r"\1", wanted.group(1)).lower()
                                 in s["title"].lower())]
                    rows.sort(key=lambda s: s["modified"], reverse=True)
                    start = int(query.get("pageToken", ["0"])[0])
                    size = int(query.get("pageSize", ["100"])[0])
                    answer: Dict[str, Any] = {"files": [
                        {"id": s["id"], "name": s["title"], "modifiedTime": s["modified"],
                         "webViewLink": f"https://docs.google.com/spreadsheets/d/{s['id']}/edit",
                         "owners": [{"emailAddress": stub.ACCOUNT}]}
                        for s in rows[start:start + size]]}
                    if start + size < len(rows):
                        answer["nextPageToken"] = str(start + size)
                    return self._json(200, answer)
                spreadsheet = self._spreadsheet(segments)
                if spreadsheet is None:
                    return self._error(404, "Requested entity was not found.")
                try:
                    if len(segments) == 5 and segments[4] == "values:batchGet":
                        return self._json(200, {"spreadsheetId": spreadsheet["id"], "valueRanges": [
                            stub._read(spreadsheet, r) for r in query.get("ranges", [])]})
                    if len(segments) == 4:
                        return self._json(200, {
                            "spreadsheetId": spreadsheet["id"],
                            "properties": {"title": spreadsheet["title"]},
                            "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{spreadsheet['id']}/edit",
                            "sheets": [{"properties": {
                                "sheetId": t["sheet_id"], "title": t["title"], "index": i,
                                "gridProperties": {"rowCount": t["grid_rows"],
                                                   "columnCount": t["grid_columns"]}}}
                                for i, t in enumerate(spreadsheet["tabs"])]})
                    if len(segments) == 6 and segments[4] == "values":
                        return self._json(200, stub._read(spreadsheet, segments[5]))
                except RangeError as exc:
                    return self._error(400, str(exc))
                self._error(404, f"no route {path}")

            def do_POST(self):  # noqa: N802
                segments, query = self._route()
                if not self._authed():
                    return
                spreadsheet = self._spreadsheet(segments)
                if spreadsheet is None:
                    return self._error(404, "Requested entity was not found.")
                if len(segments) != 6 or segments[4] != "values" or not segments[5].endswith(":append"):
                    return self._error(404, "no route")
                body = self._body()
                stub.writes.append("append")
                if self._drop():
                    return
                try:
                    tab, *_ = stub._parse(spreadsheet, segments[5][:-len(":append")])
                except RangeError as exc:
                    return self._error(400, str(exc))
                if query.get("insertDataOption", ["OVERWRITE"])[0] != "INSERT_ROWS":
                    return self._error(400, "this stub only inserts rows")
                rows = [[str(c) for c in r] for r in body.get("values") or []]
                last = stub._last_row(tab)
                table = f"{stub._name(tab)}!A1:{letter(max([len(r) for r in tab['cells']] + [1]))}{last}"
                tab["cells"][last:last] = rows
                tab["grid_rows"] += len(rows)
                width = max(len(r) for r in rows)
                self._json(200, {"spreadsheetId": spreadsheet["id"], "tableRange": table, "updates": {
                    "spreadsheetId": spreadsheet["id"],
                    "updatedRange": f"{stub._name(tab)}!A{last + 1}:{letter(width)}{last + len(rows)}",
                    "updatedRows": len(rows), "updatedColumns": width,
                    "updatedCells": sum(len(r) for r in rows)}})

            def do_PUT(self):  # noqa: N802
                segments, query = self._route()
                if not self._authed():
                    return
                spreadsheet = self._spreadsheet(segments)
                if spreadsheet is None:
                    return self._error(404, "Requested entity was not found.")
                if len(segments) != 6 or segments[4] != "values":
                    return self._error(404, "no route")
                body = self._body()
                stub.writes.append("update")
                if self._drop():
                    return
                try:
                    tab, top, left, bottom, right, explicit = stub._parse(spreadsheet, segments[5])
                except RangeError as exc:
                    return self._error(400, str(exc))
                rows = [[str(c) for c in r] for r in body.get("values") or []]
                if explicit and (top + len(rows) - 1 > bottom
                                 or left + max(len(r) for r in rows) - 1 > right):
                    return self._error(400, f"Requested writing within range [{segments[5]}], "
                                            f"but tried writing beyond it.")
                for i, row in enumerate(rows):
                    r = top - 1 + i
                    while len(tab["cells"]) <= r:
                        tab["cells"].append([])
                    target = tab["cells"][r]
                    while len(target) < left - 1 + len(row):
                        target.append("")
                    target[left - 1:left - 1 + len(row)] = row
                width = max(len(r) for r in rows)
                self._json(200, {
                    "spreadsheetId": spreadsheet["id"],
                    "updatedRange": f"{stub._name(tab)}!{letter(left)}{top}:"
                                    f"{letter(left + width - 1)}{top + len(rows) - 1}",
                    "updatedRows": len(rows), "updatedColumns": width,
                    "updatedCells": sum(len(r) for r in rows)})

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
