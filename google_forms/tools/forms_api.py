"""A small Google Forms (v1) client over the connected account.

The same shape as the other Google agents' clients, and the same failure
kinds — ``auth`` (reconnect), ``http`` (Google refused), ``not_found``.
This agent only reads, so every request is retried once on a dropped
connection.

Forms are FOUND through the Drive API, because the Forms API has no list
of forms; in tests one ``api_base_url`` stands in for both.

Two Forms specifics. The API promises no order for responses, so this
client reads every page and the agent sorts them itself. And a response
carries its answers by question id only; the titles a person recognises
come from the form, which is read alongside.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

FORMS_BASE_URL = "https://forms.googleapis.com"
DRIVE_BASE_URL = "https://www.googleapis.com"
READ_TIMEOUT = 20

FORM = "application/vnd.google-apps.form"
#: The Forms API's largest page.
PAGE_SIZE = 5000
#: Every page is read so responses can be put in order; past this many
#: the reading stops and the result says it was cut short.
MAX_RESPONSES = 20000


class GoogleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "not_found"
        self.message = message


def literal(text: str) -> str:
    """A value inside a Drive query: quoted, with quote and backslash escaped."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def form_path(form_id: str) -> str:
    return "v1/forms/" + urllib.parse.quote(str(form_id), safe="")


def moment(stamp: str) -> str:
    """An RFC 3339 UTC time that sorts as text. Google writes anywhere
    from no fraction to nanoseconds ("…:30Z", "…:30.123456Z"), and as
    plain text "…:30.1Z" sorts before "…:30Z"; padding the fraction to
    nine digits puts them in time order. Anything unreadable sorts
    first."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$", str(stamp or ""))
    if not match:
        return ""
    return f"{match.group(1)}.{(match.group(2) or '').ljust(9, '0')}Z"


class FormsClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        override = str(secret.get("api_base_url") or "").rstrip("/")
        self.forms_base_url = override or FORMS_BASE_URL
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
    def _request(self, base: str, path: str, params=None) -> Dict[str, Any]:
        url = f"{base}/{path.lstrip('/')}"
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                response = requests.get(url, params=params, timeout=READ_TIMEOUT,
                                        headers={"Authorization": f"Bearer {self._token()}"})
            except requests.RequestException as exc:
                last = exc
                continue
            if response.status_code == 401:
                raise GoogleError("auth", "The Google connection has expired or "
                                          "was revoked — reconnect the account "
                                          "from its Credentials page.")
            if response.status_code == 404:
                raise GoogleError("not_found", "Google has no such form, or this "
                                               "account cannot open it.")
            if response.status_code >= 400:
                raise GoogleError("http", f"Google Forms refused GET {path}: "
                                          f"{self._detail(response)}")
            return response.json() if response.content else {}
        raise GoogleError("http", f"Google Forms could not be reached: {last}")

    # -- Drive: who and where ----------------------------------------------
    def about(self) -> Dict[str, Any]:
        return self._request(self.drive_base_url, "drive/v3/about",
                             params={"fields": "user(emailAddress)"})

    def find(self, name: str, page_size: int, page_token: str = "") -> Dict[str, Any]:
        clauses = [f"mimeType = '{FORM}'", "trashed = false"]
        if name:
            clauses.append(f"name contains {literal(name)}")
        params: Dict[str, Any] = {
            "q": " and ".join(clauses), "pageSize": page_size,
            "orderBy": "modifiedTime desc",
            "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink,"
                      "owners(emailAddress))"}
        if page_token:
            params["pageToken"] = page_token
        return self._request(self.drive_base_url, "drive/v3/files", params=params)

    # -- Forms -------------------------------------------------------------
    def form(self, form_id: str) -> Dict[str, Any]:
        return self._request(self.forms_base_url, form_path(form_id))

    def responses(self, form_id: str, since: str = "") -> tuple:
        """(every response, whether reading stopped at MAX_RESPONSES).
        With since, only those submitted at or after it — Google's own
        filter, so the pages hold only what is asked for."""
        params: Dict[str, Any] = {"pageSize": PAGE_SIZE}
        if since:
            params["filter"] = f"timestamp >= {since}"
        found: List[Dict[str, Any]] = []
        while True:
            answer = self._request(self.forms_base_url, form_path(form_id) + "/responses",
                                   params=params)
            found.extend(answer.get("responses") or [])
            token = answer.get("nextPageToken")
            if not token:
                return found, False
            if len(found) >= MAX_RESPONSES:
                return found, True
            params["pageToken"] = token


class Questions:
    """A form's questions by id: the title a person sees, the kind, the
    options. A grid is one question per row, named "grid — row"."""

    KINDS = {"RADIO": "multiple_choice", "CHECKBOX": "checkboxes", "DROP_DOWN": "dropdown"}

    def __init__(self, form: Dict[str, Any]):
        self.rows: List[Dict[str, Any]] = []
        for item in form.get("items") or []:
            title = str(item.get("title") or "").strip()
            if "questionItem" in item:
                question = (item["questionItem"] or {}).get("question") or {}
                self._add(question, title)
            elif "questionGroupItem" in item:
                group = item["questionGroupItem"] or {}
                columns = ((group.get("grid") or {}).get("columns") or {})
                options = [str(o.get("value") or "") for o in columns.get("options") or []]
                for question in group.get("questions") or []:
                    row = str(((question.get("rowQuestion") or {}).get("title")) or "").strip()
                    self._add(question, f"{title} — {row}" if row else title,
                              kind="grid", options=options)
        self.titles = self._unique()

    def _add(self, question: Dict[str, Any], title: str, kind: str = "", options=None) -> None:
        if not kind:
            kind, options = self._kind(question)
        self.rows.append({"question_id": str(question.get("questionId") or ""),
                          "title": title, "kind": kind, "options": options or [],
                          "required": bool(question.get("required"))})

    @classmethod
    def _kind(cls, question: Dict[str, Any]):
        if "choiceQuestion" in question:
            choice = question["choiceQuestion"] or {}
            options = [("Other" if o.get("isOther") else str(o.get("value") or ""))
                       for o in choice.get("options") or []]
            return cls.KINDS.get(str(choice.get("type") or ""), "choice"), options
        if "textQuestion" in question:
            return ("paragraph" if (question["textQuestion"] or {}).get("paragraph")
                    else "short_text"), []
        if "scaleQuestion" in question:
            scale = question["scaleQuestion"] or {}
            return "scale", [str(n) for n in range(int(scale.get("low") or 0),
                                                   int(scale.get("high") or 0) + 1)]
        for key, kind in (("dateQuestion", "date"), ("timeQuestion", "time"),
                          ("fileUploadQuestion", "file_upload"),
                          ("ratingQuestion", "rating"), ("rowQuestion", "grid")):
            if key in question:
                return kind, []
        return "other", []

    def _unique(self) -> Dict[str, str]:
        """question id → a title no other question in the form has."""
        titles, seen = {}, {}
        for row in self.rows:
            title = row["title"] or "Untitled question"
            seen[title] = seen.get(title, 0) + 1
            if seen[title] > 1:
                title = f"{title} ({seen[title]})"
            titles[row["question_id"]] = title
        return titles
