"""A small Microsoft Graph workbook client over the connected account.

One class, no SDK, and the same failure kinds as the other Microsoft
agents' clients:

- ``auth``: the account is not connected, or Microsoft no longer
  accepts its grant — reconnect it from the Credentials page.
- ``http``: Graph answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it.
- ``not_found``: no such workbook, worksheet or table.

Reads are retried once on a dropped connection; writes never are.

A workbook is a drive item, and everything inside it lives under
``/me/drive/items/{id}/workbook``. Worksheets and tables are addressed
by name, escaped; ranges by A1 address inside one worksheet.

Cells are read as their TEXT — what a person sees in Excel — because a
date's value is a serial number (46269.39) and an amount's is a bare
float, while the text is the date and the amount as formatted. The one
exception is the previous content of a range about to be overwritten,
which is read as formulas: that is what would put it back.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30

#: The widest block this agent reads. A sheet can be 16,384 columns
#: wide; a result the model reads cannot.
MAX_COLUMNS = 50
#: The longest cell text handed on; the rest is cut with an ellipsis.
MAX_CELL_CHARS = 300

CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def quoted(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


# -- A1 addresses ----------------------------------------------------------

def column_number(letters: str) -> int:
    number = 0
    for ch in letters.upper():
        number = number * 26 + (ord(ch) - 64)
    return number


def column_letters(number: int) -> str:
    letters = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def split_address(address: str) -> Tuple[str, str]:
    """ "'Q3 Orders'!A1:D9" as ("Q3 Orders", "A1:D9"); no sheet is ""."""
    text = str(address or "").strip()
    if "!" not in text:
        return "", text
    sheet, _, local = text.rpartition("!")
    if len(sheet) >= 2 and sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, local


def parse_box(local: str) -> Optional[Tuple[int, int, int, int]]:
    """ "B2:D9" as (first row, first column, last row, last column), or
    None for anything that is not one cell or one block of cells —
    whole columns ("A:D") included, since those are a million rows."""
    parts = [p.strip() for p in str(local or "").split(":")]
    if len(parts) not in (1, 2):
        return None
    cells = [CELL.match(p) for p in parts]
    if not all(cells):
        return None
    points = [(int(m.group(2)), column_number(m.group(1))) for m in cells]
    if len(points) == 1:
        points.append(points[0])
    (ra, ca), (rb, cb) = points
    if min(ra, rb) < 1 or max(ca, cb) > 16384:
        return None
    return min(ra, rb), min(ca, cb), max(ra, rb), max(ca, cb)


def box_address(r1: int, c1: int, r2: int, c2: int) -> str:
    return f"{column_letters(c1)}{r1}:{column_letters(c2)}{r2}"


# -- cells -----------------------------------------------------------------

def clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS - 1] + "…"
    return value


def blank(line: List[Any]) -> bool:
    return all(str(cell if cell is not None else "").strip() == "" for cell in line)


def header_names(cells: List[Any]) -> List[str]:
    """A header row as names a row can be keyed by: a blank header is
    "Column 3", and a repeated one "Amount (2)", so no value is lost to
    another under the same key."""
    names: List[str] = []
    for i, cell in enumerate(cells, start=1):
        name = str(cell if cell is not None else "").strip() or f"Column {i}"
        candidate, n = name, 2
        while candidate in names:
            candidate = f"{name} ({n})"
            n += 1
        names.append(candidate)
    return names


def keyed(headers: List[str], line: List[Any]) -> Dict[str, Any]:
    return {h: clip(line[i] if i < len(line) else "") for i, h in enumerate(headers)}


class Region:
    """Rows under a header row: a table, or a worksheet's used range read
    as one. Data rows are counted from 0 below the header; ``address``
    turns a slice of them into the A1 block to read."""

    def __init__(self, kind: str, name: str, worksheet: str, header_row: int,
                 first_col: int, last_col: int, last_row: int, headers: List[str],
                 columns_cut: int = 0):
        self.kind = kind              # "table" | "worksheet"
        self.name = name
        self.worksheet = worksheet
        self.header_row = header_row
        self.first_col = first_col
        self.last_col = last_col
        self.last_row = last_row
        self.headers = headers
        self.columns_cut = columns_cut

    @property
    def rows(self) -> int:
        return max(0, self.last_row - self.header_row)

    def row_number(self, index: int) -> int:
        """The worksheet row a data row sits on, as Excel numbers it."""
        return self.header_row + 1 + index

    def address(self, start: int, stop: int) -> str:
        return box_address(self.row_number(start), self.first_col,
                           self.row_number(stop - 1), self.last_col)


class WorkbookClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        self.api_base_url = (str(secret.get("api_base_url") or "")
                             or API_BASE_URL).rstrip("/")

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise GraphError("auth", "The Microsoft account is not connected — "
                                     "connect it from the agent's Credentials "
                                     "tab.")
        return self.access_token

    @staticmethod
    def _detail(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:300]
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)[:300]
        return str(error or body)[:300]

    # -- one request -----------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json=None,
                 write: bool = False, session: str = ""):
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            if session:
                headers["workbook-session-id"] = session
            try:
                response = requests.request(
                    method, url, params=params, json=json, headers=headers,
                    timeout=WRITE_TIMEOUT if write else READ_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GraphError(
                        "unknown", f"No answer from Microsoft for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GraphError("auth", "The Microsoft connection has expired "
                                         "or was revoked — reconnect the "
                                         "account from its Credentials page.")
            if response.status_code == 404:
                raise GraphError("not_found", "OneDrive has no such workbook, "
                                              "worksheet or table.")
            if response.status_code >= 400:
                raise GraphError("http", f"Microsoft refused {method} {path}: "
                                         f"{self._detail(response)}")
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise GraphError("http", f"Microsoft could not be reached: {last}")

    @staticmethod
    def _workbook(item_id: str) -> str:
        return f"me/drive/items/{quoted(item_id)}/workbook"

    def _sheet(self, item_id: str, sheet: str) -> str:
        return f"{self._workbook(item_id)}/worksheets/{quoted(sheet)}"

    def _table(self, item_id: str, table: str) -> str:
        return f"{self._workbook(item_id)}/tables/{quoted(table)}"

    # -- the drive -------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "me", params={"$select": "mail,userPrincipalName"})

    def item(self, item_id: str) -> Dict[str, Any]:
        return self._request("GET", f"me/drive/items/{quoted(item_id)}")

    def search(self, query: str, top: int) -> List[Dict[str, Any]]:
        escaped = quoted(str(query).replace("'", "''"))
        return self._request("GET", f"me/drive/root/search(q='{escaped}')",
                             params={"$top": top}).get("value") or []

    # -- inside a workbook -----------------------------------------------
    def worksheets(self, item_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", self._workbook(item_id) + "/worksheets",
                             params={"$select": "id,name,position,visibility"}
                             ).get("value") or []

    def tables(self, item_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", self._workbook(item_id) + "/tables",
                             params={"$select": "id,name,showHeaders,showTotals"}
                             ).get("value") or []

    def used_range(self, item_id: str, sheet: str) -> Dict[str, Any]:
        """The block holding values. valuesOnly leaves out cells that are
        only formatted, which would otherwise stretch it to row 1000."""
        return self._request("GET", self._sheet(item_id, sheet) + "/usedRange(valuesOnly=true)",
                             params={"$select": "address,rowCount,columnCount"})

    def cells(self, item_id: str, sheet: str, address: str,
              field: str = "text") -> List[List[Any]]:
        """One block's cells as rows of lists: text by default, or
        "values" / "formulas"."""
        path = f"{self._sheet(item_id, sheet)}/range(address='{quoted(address)}')"
        return self._request("GET", path, params={"$select": field}).get(field) or []

    def table_region(self, item_id: str, table: str) -> Region:
        found = self._request("GET", self._table(item_id, table),
                              params={"$select": "id,name,showHeaders,showTotals"})
        box = self._request("GET", self._table(item_id, table) + "/range",
                            params={"$select": "address"})
        sheet, local = split_address(box.get("address"))
        parsed = parse_box(local)
        if parsed is None:
            raise GraphError("http", f"Microsoft gave table {table} an address "
                                     f"this agent cannot read: {box.get('address')!r}.")
        r1, c1, r2, c2 = parsed
        shown_c2 = min(c2, c1 + MAX_COLUMNS - 1)
        has_header = found.get("showHeaders") is not False
        if has_header:
            header_cells = (self.cells(item_id, sheet, box_address(r1, c1, r1, shown_c2))
                            or [[]])[0]
            header_row = r1
        else:
            header_cells, header_row = [""] * (shown_c2 - c1 + 1), r1 - 1
        region = Region("table", str(found.get("name") or table), sheet, header_row,
                        c1, shown_c2, r2 - 1 if found.get("showTotals") else r2,
                        header_names(header_cells), columns_cut=c2 - shown_c2)
        # An Excel table always keeps one body row, even with no data:
        # a new table, or one whose rows were all deleted, shows a single
        # blank row. That row is not a row of data, and counting it would
        # make the first real row look like it was always there.
        if region.rows == 1:
            first = self.read_rows(item_id, region, 0, 1)
            if not first or blank(first[0]):
                region.last_row = region.header_row
        return region

    def sheet_region(self, item_id: str, sheet: str) -> Region:
        """A worksheet's used range read as a table: its first row the
        header, every row under it data."""
        used = self.used_range(item_id, sheet)
        _, local = split_address(used.get("address"))
        parsed = parse_box(local)
        if parsed is None:
            raise GraphError("http", f"Microsoft gave worksheet {sheet} a used range "
                                     f"this agent cannot read: {used.get('address')!r}.")
        r1, c1, r2, c2 = parsed
        shown_c2 = min(c2, c1 + MAX_COLUMNS - 1)
        header_cells = (self.cells(item_id, sheet, box_address(r1, c1, r1, shown_c2))
                        or [[]])[0]
        if r1 == r2 and blank(header_cells):
            # An empty sheet's used range is A1, blank: no header, no rows.
            return Region("worksheet", sheet, sheet, r1, c1, shown_c2, r1, [])
        return Region("worksheet", sheet, sheet, r1, c1, shown_c2, r2,
                      header_names(header_cells), columns_cut=c2 - shown_c2)

    def read_rows(self, item_id: str, region: Region, start: int, stop: int) -> List[List[Any]]:
        """Data rows [start, stop) of a region, as text."""
        stop = min(stop, region.rows)
        if stop <= start:
            return []
        return self.cells(item_id, region.worksheet, region.address(start, stop))

    # -- writes ----------------------------------------------------------
    def create_session(self, item_id: str) -> str:
        """A persistent session, so a write lands as one change in the
        workbook rather than racing whoever else has it open."""
        answer = self._request("POST", self._workbook(item_id) + "/createSession",
                               json={"persistChanges": True}, write=True)
        return str(answer.get("id") or "")

    def close_session(self, item_id: str, session: str) -> None:
        """Best effort: an unclosed session expires on Microsoft's side
        within minutes, and the write it carried has already landed."""
        if not session:
            return
        try:
            self._request("POST", self._workbook(item_id) + "/closeSession",
                          json={}, write=True, session=session)
        except GraphError:
            pass

    def add_table_rows(self, item_id: str, table: str, values: List[List[Any]],
                       session: str) -> Dict[str, Any]:
        return self._request("POST", self._table(item_id, table) + "/rows/add",
                             json={"index": None, "values": values},
                             write=True, session=session)

    def set_range(self, item_id: str, sheet: str, address: str,
                  values: List[List[Any]], session: str) -> Dict[str, Any]:
        path = f"{self._sheet(item_id, sheet)}/range(address='{quoted(address)}')"
        return self._request("PATCH", path, json={"values": values},
                             write=True, session=session)


def workbook_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """A drive item as the agent shows a workbook."""
    parent = item.get("parentReference") or {}
    path = str(parent.get("path") or "")
    return {
        "item_id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "folder": (path.split("root:", 1)[1] or "/") if "root:" in path else "",
        "modified": str(item.get("lastModifiedDateTime") or ""),
        "size": int(item.get("size") or 0),
        "link": str(item.get("webUrl") or ""),
    }


def is_workbook(item: Dict[str, Any]) -> bool:
    return "file" in item and str(item.get("name") or "").lower().endswith(".xlsx")
