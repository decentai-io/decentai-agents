"""A small Microsoft Graph To Do client over the connected account.

One class, no SDK, and the same failure kinds as the Outlook and
Microsoft Calendar agents:

- ``auth``: the account is not connected, or Microsoft no longer
  accepts its grant — reconnect it from the Credentials page.
- ``http``: Graph answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it.
- ``not_found``: Graph has no such list or task.

Reads are retried once on a dropped connection; writes never are.

To Do ids are long base64 strings that may carry "=" and "/", so every
id is escaped before it goes into a path.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def quoted(identifier: str) -> str:
    return urllib.parse.quote(str(identifier), safe="")


def list_path(list_id: str) -> str:
    return "me/todo/lists/" + quoted(list_id)


def task_path(list_id: str, task_id: str) -> str:
    return list_path(list_id) + "/tasks/" + quoted(task_id)


def second(text: Any) -> str:
    """Any ISO 8601 moment as UTC to the second, with Z — the shape a
    cursor is kept and compared in. Graph writes seven fraction digits,
    which the standard library will not parse and a cursor does not
    need. Empty when the text is not a date-time. A naive time is read
    as UTC: the cursor is compared with Graph's clock, not a person's."""
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


def graph_date(value: Any) -> str:
    """A {dateTime, timeZone} as its YYYY-MM-DD. The agent writes due
    dates as midnight UTC, so the date part is the date meant."""
    return str((value or {}).get("dateTime") or "")[:10]


def graph_moment(value: Any) -> str:
    """A {dateTime, timeZone} written in UTC, as ISO 8601 with Z."""
    text = str((value or {}).get("dateTime") or "")
    return second(text) if text else ""


class TodoClient:
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
                 write: bool = False, absolute: str = "") -> Dict[str, Any]:
        url = absolute or f"{self.api_base_url}/{path.lstrip('/')}"
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
                    raise GraphError(
                        "unknown", f"No answer from Microsoft for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GraphError("auth", "The Microsoft connection has expired "
                                         "or was revoked — reconnect the "
                                         "account from its Credentials page.")
            if response.status_code == 404:
                raise GraphError("not_found", "Microsoft To Do has no such list "
                                              "or task.")
            if response.status_code >= 400:
                raise GraphError("http", f"Microsoft refused {method} {path or url}: "
                                         f"{self._detail(response)}")
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise GraphError("http", f"Microsoft could not be reached: {last}")

    def follows(self, page_link: str) -> bool:
        """A page token is Graph's own next-link. Only one pointing back at
        Graph is followed, so a token can never carry the access token
        somewhere else."""
        return str(page_link).startswith(self.api_base_url + "/")

    # -- reads -----------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "me", params={"$select": "mail,userPrincipalName,displayName"})

    def lists(self, most: int) -> tuple:
        """Up to ``most`` task lists, and whether there are more."""
        page = self._request("GET", "me/todo/lists")
        rows = list(page.get("value") or [])
        while page.get("@odata.nextLink") and len(rows) <= most:
            page = self._request("GET", "", absolute=str(page["@odata.nextLink"]))
            rows.extend(page.get("value") or [])
        return rows[:most], len(rows) > most

    def all_lists(self) -> List[Dict[str, Any]]:
        rows, _ = self.lists(100)
        return rows

    def get_list(self, list_id: str) -> Dict[str, Any]:
        return self._request("GET", list_path(list_id))

    def default_list(self) -> Optional[Dict[str, Any]]:
        for row in self.all_lists():
            if row.get("wellknownListName") == "defaultList":
                return row
        return None

    def tasks(self, list_id: str, *, filters: List[str], order: str, top: int,
              page_link: str = "") -> Dict[str, Any]:
        """One page of a list's tasks. Graph pages with a full next-link
        URL, which is what travels as the page token."""
        if page_link:
            return self._request("GET", "", absolute=page_link)
        params: Dict[str, Any] = {"$orderby": order, "$top": top}
        if filters:
            params["$filter"] = " and ".join(filters)
        return self._request("GET", list_path(list_id) + "/tasks", params=params)

    def changed_since(self, list_id: str, since: str, top: int,
                      completed_only: bool = False) -> List[Dict[str, Any]]:
        """Tasks modified at or after ``since`` (ISO 8601, UTC), oldest
        change first, at most ``top``. Graph wants the property it orders
        by to lead the filter — lastModifiedDateTime is both."""
        filters = [f"lastModifiedDateTime ge {since}"]
        if completed_only:
            filters.append("status eq 'completed'")
        page = self.tasks(list_id, filters=filters,
                          order="lastModifiedDateTime asc", top=min(top, 100))
        return list(page.get("value") or [])

    def newest(self, list_id: str, top: int = 10) -> List[Dict[str, Any]]:
        """The most recently modified tasks of a list — where a watch
        starts, in Graph's own clock."""
        page = self.tasks(list_id, filters=[], order="lastModifiedDateTime desc", top=top)
        return list(page.get("value") or [])

    def get_task(self, list_id: str, task_id: str) -> Dict[str, Any]:
        return self._request("GET", task_path(list_id, task_id))

    def checklist(self, list_id: str, task_id: str) -> List[Dict[str, Any]]:
        page = self._request("GET", task_path(list_id, task_id) + "/checklistItems")
        return list(page.get("value") or [])

    # -- writes ----------------------------------------------------------
    def create_list(self, name: str) -> Dict[str, Any]:
        return self._request("POST", "me/todo/lists", json={"displayName": name}, write=True)

    def create_task(self, list_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", list_path(list_id) + "/tasks", json=task, write=True)

    def update_task(self, list_id: str, task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", task_path(list_id, task_id), json=patch, write=True)

    def delete_task(self, list_id: str, task_id: str) -> None:
        self._request("DELETE", task_path(list_id, task_id), write=True)
