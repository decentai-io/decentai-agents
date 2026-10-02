"""A small Google Tasks client over the connected account.

The same shape as the Google Calendar agent's client, and the same
failure kinds — ``auth`` (reconnect), ``http`` (Google refused),
``unknown`` (a write got no answer; never retried), ``not_found``.
Reads are retried once on a dropped connection; writes never are.

Google Tasks cannot be asked for an order: a list comes back in the
person's own arrangement. So whatever must be sorted — newest change
first, oldest completion first — is read whole, a bounded number of
pages, and sorted here.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://tasks.googleapis.com/tasks/v1"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30

#: The most a list is read to sort it: ten pages of a hundred. A list
#: longer than that is sorted by its first thousand tasks.
MAX_PAGES = 10


class GoogleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def quoted(identifier: str) -> str:
    return urllib.parse.quote(str(identifier), safe="@")


def second(text: Any) -> str:
    """Any RFC 3339 moment as UTC to the second, with Z — the shape a
    cursor is kept and compared in. Google writes milliseconds, which a
    cursor does not need. Empty when the text is not a date-time; a
    naive time is read as UTC, because the cursor is compared with
    Google's clock, not a person's."""
    text = str(text or "").strip()
    if not text:
        return ""
    text = re.sub(r"\.\d+", "", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TasksClient:
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
            return str(error.get("message") or error)[:300]
        return str(error or body)[:300]

    def _request(self, method: str, path: str, *, params=None, json=None,
                 write: bool = False) -> Dict[str, Any]:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = requests.request(
                    method, url, params=params, json=json,
                    headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GoogleError(
                        "unknown", f"No answer from Google Tasks for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GoogleError("auth", "The Google connection has expired or "
                                          "was revoked — reconnect the account "
                                          "from its Credentials page.")
            if response.status_code in (404, 410):
                raise GoogleError("not_found", "Google Tasks has no such list or task.")
            if response.status_code >= 400:
                raise GoogleError("http", f"Google Tasks refused {method} {path}: "
                                          f"{self._detail(response)}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GoogleError("http", f"Google Tasks could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def lists(self, most: int) -> tuple:
        """Up to ``most`` task lists, and whether there are more."""
        rows: List[Dict[str, Any]] = []
        token = ""
        for _ in range(MAX_PAGES):
            params: Dict[str, Any] = {"maxResults": 100}
            if token:
                params["pageToken"] = token
            page = self._request("GET", "users/@me/lists", params=params)
            rows.extend(page.get("items") or [])
            token = str(page.get("nextPageToken") or "")
            if not token or len(rows) > most:
                break
        return rows[:most], len(rows) > most or bool(token)

    def all_lists(self) -> List[Dict[str, Any]]:
        rows, _ = self.lists(100)
        return rows

    def get_list(self, list_id: str) -> Dict[str, Any]:
        """One list; "@default" names the account's default list."""
        return self._request("GET", "users/@me/lists/" + quoted(list_id))

    def all_tasks(self, list_id: str, **filters: Any) -> List[Dict[str, Any]]:
        """Every task of a list matching ``filters`` (Google's own query
        parameters), in Google's order, at most MAX_PAGES pages."""
        rows: List[Dict[str, Any]] = []
        token = ""
        for _ in range(MAX_PAGES):
            params: Dict[str, Any] = {"maxResults": 100, **filters}
            if token:
                params["pageToken"] = token
            page = self._request("GET", f"lists/{quoted(list_id)}/tasks", params=params)
            rows.extend(page.get("items") or [])
            token = str(page.get("nextPageToken") or "")
            if not token:
                break
        return rows

    def get_task(self, list_id: str, task_id: str) -> Dict[str, Any]:
        return self._request("GET", f"lists/{quoted(list_id)}/tasks/{quoted(task_id)}")

    # -- writes ----------------------------------------------------------
    def create_list(self, title: str) -> Dict[str, Any]:
        return self._request("POST", "users/@me/lists", json={"title": title}, write=True)

    def create_task(self, list_id: str, task: Dict[str, Any], parent: str = "") -> Dict[str, Any]:
        params = {"parent": parent} if parent else None
        return self._request("POST", f"lists/{quoted(list_id)}/tasks",
                             params=params, json=task, write=True)

    def update_task(self, list_id: str, task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", f"lists/{quoted(list_id)}/tasks/{quoted(task_id)}",
                             json=patch, write=True)

    def delete_task(self, list_id: str, task_id: str) -> None:
        self._request("DELETE", f"lists/{quoted(list_id)}/tasks/{quoted(task_id)}",
                      write=True)
