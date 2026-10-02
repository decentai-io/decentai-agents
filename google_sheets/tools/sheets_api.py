"""A small Google Sheets (v4) client over the connected account.

The same shape as the Google Drive agent's client, and the same failure
kinds — ``auth`` (reconnect), ``http`` (Google refused), ``unknown`` (a
write got no answer; never retried) — plus ``not_found``. Reads are
retried once on a dropped connection; writes never are.

Two services answer here. Spreadsheets are read and written through the
Sheets API; they are FOUND through the Drive API, because Sheets has no
list of its own. In tests one ``api_base_url`` stands in for both — the
paths do not overlap.

Three Sheets specifics. A tab is addressed by its title inside an A1
range, quoted, so a tab called "Sign-ups 2026" or "Dana's list" still
parses. A read beyond the tab's grid is refused by Google ("exceeds grid
limits"), so every range this client builds is clamped to the grid. And
values come back trimmed: trailing empty rows and cells are left out,
while an empty row in the middle comes back as an empty list.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

import requests

SHEETS_BASE_URL = "https://sheets.googleapis.com"
DRIVE_BASE_URL = "https://www.googleapis.com"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30

SPREADSHEET = "application/vnd.google-apps.spreadsheet"
SHEET_FIELDS = ("spreadsheetId,properties.title,spreadsheetUrl,"
                "sheets.properties(sheetId,title,index,gridProperties(rowCount,columnCount))")


class GoogleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def literal(text: str) -> str:
    """A value inside a Drive query: quoted, with quote and backslash escaped."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def column_letter(number: int) -> str:
    """1 → A, 26 → Z, 27 → AA."""
    letters = ""
    number = max(1, int(number))
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def column_number(letters: str) -> int:
    """A → 1, Z → 26, AA → 27."""
    number = 0
    for letter in letters.upper():
        number = number * 26 + (ord(letter) - 64)
    return number


def a1(tab: str, cells: str = "") -> str:
    """'Sign-ups'!A1:F20 — the tab always quoted, a quote in it doubled."""
    quoted = "'" + str(tab).replace("'", "''") + "'"
    return f"{quoted}!{cells}" if cells else quoted


def spreadsheet_path(spreadsheet_id: str) -> str:
    return "v4/spreadsheets/" + urllib.parse.quote(str(spreadsheet_id), safe="")


class SheetsClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        override = str(secret.get("api_base_url") or "").rstrip("/")
        self.sheets_base_url = override or SHEETS_BASE_URL
        self.drive_base_url = override or DRIVE_BASE_URL

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise GoogleError("auth", "The Google account is not connected — "
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
            return str(error.get("message") or error.get("status") or error)[:300]
        return str(error or body)[:300]

    # -- one request -----------------------------------------------------
    def _request(self, method: str, base: str, path: str, *, params=None,
                 json_body=None, write: bool = False):
        url = f"{base}/{path.lstrip('/')}"
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = requests.request(
                    method, url, params=params, json=json_body, headers=headers,
                    timeout=WRITE_TIMEOUT if write else READ_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GoogleError(
                        "unknown", f"No answer from Google Sheets for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GoogleError("auth", "The Google connection has expired or "
                                          "was revoked — reconnect the account "
                                          "from its Credentials page.")
            if response.status_code == 404:
                raise GoogleError("not_found", "Google has no such spreadsheet, or "
                                               "this account cannot open it.")
            if response.status_code >= 400:
                raise GoogleError("http", f"Google Sheets refused {method} {path}: "
                                          f"{self._detail(response)}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GoogleError("http", f"Google Sheets could not be reached: {last}")

    # -- Drive: who and where ----------------------------------------------
    def about(self) -> Dict[str, Any]:
        return self._request("GET", self.drive_base_url, "drive/v3/about",
                             params={"fields": "user(emailAddress)"})

    def find(self, name: str, page_size: int, page_token: str = "") -> Dict[str, Any]:
        clauses = [f"mimeType = '{SPREADSHEET}'", "trashed = false"]
        if name:
            clauses.append(f"name contains {literal(name)}")
        params: Dict[str, Any] = {
            "q": " and ".join(clauses), "pageSize": page_size,
            "orderBy": "modifiedTime desc",
            "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink,"
                      "owners(emailAddress))"}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", self.drive_base_url, "drive/v3/files", params=params)

    # -- Sheets: reads -----------------------------------------------------
    def spreadsheet(self, spreadsheet_id: str) -> Dict[str, Any]:
        return self._request("GET", self.sheets_base_url, spreadsheet_path(spreadsheet_id),
                             params={"fields": SHEET_FIELDS})

    def values(self, spreadsheet_id: str, range_a1: str) -> List[List[str]]:
        answer = self._request(
            "GET", self.sheets_base_url,
            spreadsheet_path(spreadsheet_id) + "/values/" + urllib.parse.quote(range_a1, safe=""),
            params={"majorDimension": "ROWS"})
        return answer.get("values") or []

    def batch_values(self, spreadsheet_id: str, ranges: List[str]) -> List[List[List[str]]]:
        """Several ranges in one request, in the order asked."""
        answer = self._request(
            "GET", self.sheets_base_url, spreadsheet_path(spreadsheet_id) + "/values:batchGet",
            params={"ranges": ranges, "majorDimension": "ROWS"})
        return [r.get("values") or [] for r in answer.get("valueRanges") or []]

    # -- Sheets: writes ----------------------------------------------------
    def append(self, spreadsheet_id: str, tab: str, rows: List[List[str]]) -> Dict[str, Any]:
        """New rows after the tab's table. INSERT_ROWS rather than the
        default OVERWRITE: an append must never land on cells that
        already hold something further down the tab."""
        return self._request(
            "POST", self.sheets_base_url,
            spreadsheet_path(spreadsheet_id) + "/values/"
            + urllib.parse.quote(a1(tab), safe="") + ":append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json_body={"majorDimension": "ROWS", "values": rows}, write=True)

    def update(self, spreadsheet_id: str, range_a1: str, rows: List[List[str]]) -> Dict[str, Any]:
        return self._request(
            "PUT", self.sheets_base_url,
            spreadsheet_path(spreadsheet_id) + "/values/" + urllib.parse.quote(range_a1, safe=""),
            params={"valueInputOption": "USER_ENTERED"},
            json_body={"range": range_a1, "majorDimension": "ROWS", "values": rows},
            write=True)


def tab_row(sheet: Dict[str, Any]) -> Dict[str, Any]:
    properties = sheet.get("properties") or {}
    grid = properties.get("gridProperties") or {}
    return {"tab": str(properties.get("title") or ""),
            "sheet_id": int(properties.get("sheetId") or 0),
            "index": int(properties.get("index") or 0),
            "grid_rows": int(grid.get("rowCount") or 0),
            "grid_columns": int(grid.get("columnCount") or 0)}


#: How much of one cell the model is shown. A cell holds up to 50,000
#: characters; a pasted email in one would swamp everything else.
CELL_LIMIT = 500


def clip(text: Any) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= CELL_LIMIT else text[:CELL_LIMIT] + "…"


def find_tab(meta: Dict[str, Any], tab: str) -> Optional[Dict[str, Any]]:
    """The tab the user named: its exact title first, then the same title
    in another case — never the nearest guess."""
    tabs = [tab_row(s) for s in meta.get("sheets") or []]
    exact = next((t for t in tabs if t["tab"] == tab), None)
    return exact or next((t for t in tabs if t["tab"].lower() == str(tab).lower()), None)


def no_tab(meta: Dict[str, Any], tab: str):
    names = ", ".join(repr(tab_row(s)["tab"]) for s in (meta.get("sheets") or [])[:20])
    return {"error": f"The spreadsheet has no tab named {tab!r}; its tabs are {names}.",
            "kind": "not_found"}, "error"
