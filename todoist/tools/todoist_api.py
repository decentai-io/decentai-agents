"""A small Todoist client over the connected account.

Todoist's unified API v1 (it replaced REST v2 and Sync v9). Every path
the agent calls is in this one class, so a correction to an endpoint is
a one-line change here and nowhere else.

The same failure kinds as the other agents' clients:

- ``auth``: the account is not connected, or Todoist no longer accepts
  the token (the person revoked the app) — reconnect it.
- ``not_found``: Todoist has no such task or project.
- ``http``: Todoist answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome is
  unknown; nothing retries it.

Reads are retried once on a dropped connection; writes never are.

Two v1 specifics. List endpoints answer ``{"results": [...],
"next_cursor": ...}`` — except the completed-task endpoints, which say
``items`` instead of ``results``. And no object carries a link: the web
app's links are built here from the id.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://api.todoist.com/api/v1"
WEB_URL = "https://app.todoist.com/app"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
#: Todoist's own ceiling per page.
PAGE_LIMIT = 200
#: Pages read before a "whole list" read stops. Two thousand projects,
#: labels or completions in one answer is past anything a person asks.
MAX_PAGES = 10


class TodoistError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class TodoistClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent and keeps the
        # token. Todoist tokens do not expire and there is no refresh
        # token, so nothing here refreshes; a revoked one answers 401.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        self.api_base_url = (str(secret.get("api_base_url") or "")
                             or API_BASE_URL).rstrip("/")

    # -- links -----------------------------------------------------------
    @staticmethod
    def task_link(task_id: str) -> str:
        return f"{WEB_URL}/task/{task_id}" if task_id else ""

    @staticmethod
    def project_link(project_id: str) -> str:
        return f"{WEB_URL}/project/{project_id}" if project_id else ""

    # -- transport -------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise TodoistError("auth", "The Todoist account is not connected — "
                                       "connect it from the agent's Credentials "
                                       "tab.")
        return self.access_token

    @staticmethod
    def _detail(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:300]
        if isinstance(body, dict):
            return str(body.get("error") or body)[:300]
        return str(body)[:300]

    def _request(self, method: str, path: str, *, params=None, json=None,
                 write: bool = False) -> Any:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = requests.request(method, url, params=params, json=json,
                                            headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise TodoistError(
                        "unknown", f"No answer from Todoist for {method} {path}: "
                                   f"the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise TodoistError("auth", "Todoist no longer accepts this "
                                           "connection (it was revoked) — "
                                           "reconnect the account from its "
                                           "Credentials page.")
            if response.status_code == 404:
                raise TodoistError("not_found", f"Todoist has no {path}.")
            if response.status_code >= 400:
                raise TodoistError("http", f"Todoist refused {method} {path}: "
                                           f"{self._detail(response)}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise TodoistError("http", f"Todoist could not be reached: {last}")

    def _all(self, path: str, params: Dict[str, Any], key: str = "results") -> List[Dict[str, Any]]:
        """Every page of a list endpoint, up to MAX_PAGES."""
        rows: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            page = self._request("GET", path, params={
                **params, "limit": PAGE_LIMIT, **({"cursor": cursor} if cursor else {})})
            rows.extend(page.get(key) or [])
            cursor = str(page.get("next_cursor") or "")
            if not cursor:
                break
        return rows

    # -- reads -----------------------------------------------------------
    def user(self) -> Dict[str, Any]:
        return self._request("GET", "user")

    def projects(self) -> List[Dict[str, Any]]:
        return self._all("projects", {})

    def sections(self, project_id: str = "") -> List[Dict[str, Any]]:
        return self._all("sections", {"project_id": project_id} if project_id else {})

    def labels(self) -> List[Dict[str, Any]]:
        return self._all("labels", {})

    def tasks_page(self, *, project_id: str = "", section_id: str = "",
                   label: str = "", limit: int = 20, cursor: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        for name, value in (("project_id", project_id), ("section_id", section_id),
                            ("label", label), ("cursor", cursor)):
            if value:
                params[name] = value
        return self._request("GET", "tasks", params=params)

    def filter_page(self, query: str, limit: int = 20, cursor: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"query": query, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "tasks/filter", params=params)

    def task(self, task_id: str) -> Dict[str, Any]:
        return self._request("GET", f"tasks/{task_id}")

    def comment_count(self, task_id: str) -> int:
        # A v1 task carries no comment count; the comments are counted.
        return len(self._all("comments", {"task_id": task_id}))

    def completed(self, since: str, until: str, project_id: str = "") -> List[Dict[str, Any]]:
        """Tasks completed between since and until (both inclusive, at
        most three months apart), every page. The order is Todoist's;
        callers sort."""
        params: Dict[str, Any] = {"since": since, "until": until}
        if project_id:
            params["project_id"] = project_id
        return self._all("tasks/completed/by_completion_date", params, key="items")

    # -- writes ----------------------------------------------------------
    def create_task(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "tasks", json=body, write=True)

    def update_task(self, task_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", f"tasks/{task_id}", json=body, write=True)

    def close_task(self, task_id: str) -> None:
        self._request("POST", f"tasks/{task_id}/close", write=True)

    def reopen_task(self, task_id: str) -> None:
        self._request("POST", f"tasks/{task_id}/reopen", write=True)

    def delete_task(self, task_id: str) -> None:
        self._request("DELETE", f"tasks/{task_id}", write=True)

    def create_project(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "projects", json=body, write=True)

    def add_comment(self, task_id: str, content: str) -> Dict[str, Any]:
        return self._request("POST", "comments", json={"task_id": task_id, "content": content},
                             write=True)
