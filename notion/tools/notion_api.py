"""A small Notion API client over the connected workspace.

The same shape as the drive agents' clients and the same failure kinds
— ``auth`` (reconnect), ``http`` (Notion refused), ``unknown`` (a write
got no answer; never retried), ``not_found`` — plus two Notion adds:
``not_shared`` (the integration was not given this page) and
``rate_limited`` (with Notion's Retry-After). Reads are retried once on
a dropped connection; writes never are.

The API version is pinned on every request. Newer versions reshape a
database into "data sources", which changes the paths and bodies this
agent uses; 2022-06-28 is the stable shape, chosen on purpose.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

API_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
#: Notion's own ceiling for page_size on every list endpoint.
PAGE_SIZE_MAX = 100

SHARE_HINT = ("In Notion, open the page (or a page above it), choose the ... menu, "
              "then Connections, and add this integration; or reconnect the "
              "workspace and select the page while signing in. The agent sees "
              "only what was shared with it.")


class NotionError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int = 0):
        super().__init__(message)
        # "auth" | "not_shared" | "not_found" | "rate_limited" | "http" | "unknown"
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


def quoted(notion_id: str) -> str:
    return urllib.parse.quote(str(notion_id).strip(), safe="")


class NotionClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected workspace: the platform ran the sign-in and hands
        # this agent the token. Nothing here mints or refreshes one.
        self.account = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        self.api_base_url = (str(secret.get("api_base_url") or "")
                             or API_BASE_URL).rstrip("/")

    # -- one request -----------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise NotionError("auth", "The Notion workspace is not connected; "
                                      "connect it from the agent's Credentials tab.")
        return self.access_token

    @staticmethod
    def _body(response: requests.Response) -> Dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {"message": (response.text or "").strip()[:300]}
        return body if isinstance(body, dict) else {}

    def _refusal(self, response: requests.Response, method: str, path: str) -> NotionError:
        body = self._body(response)
        code = str(body.get("code") or "")
        message = str(body.get("message") or "")[:300]
        status = response.status_code
        if status == 401:
            return NotionError("auth", "The Notion connection was revoked or has "
                                       "expired; reconnect the workspace from its "
                                       "Credentials page.")
        if status == 403 or code == "restricted_resource":
            return NotionError("not_shared", "Notion did not let the integration "
                                             "open this. " + SHARE_HINT)
        if status == 404 or code == "object_not_found":
            # Notion answers alike for "no such page" and "a page not
            # shared with you", so the hint belongs with both.
            return NotionError("not_found", "Notion found nothing with that id, or it "
                                            "is not shared with the integration. "
                                            + SHARE_HINT)
        if status == 429:
            try:
                wait = int(float(response.headers.get("Retry-After") or 0))
            except ValueError:
                wait = 0
            return NotionError("rate_limited", "Notion is limiting requests; try again "
                                               f"in {wait or 'a few'} seconds.", wait)
        if status == 409:
            return NotionError("http", "Notion reported a conflicting change to the "
                                       "same data; read it again and retry.")
        return NotionError("http", f"Notion refused {method} {path}: {message or code}")

    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 write: bool = False) -> Dict[str, Any]:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}",
                       "Notion-Version": NOTION_VERSION}
            try:
                response = requests.request(
                    method, url, params=params, json=json_body, headers=headers,
                    timeout=WRITE_TIMEOUT if write else READ_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise NotionError("unknown", f"No answer from Notion for {method} "
                                                 f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code >= 400:
                raise self._refusal(response, method, path)
            return self._body(response) if response.content else {}
        raise NotionError("http", f"Notion could not be reached: {last}")

    @staticmethod
    def _paging(page_size: int, start_cursor: str = "") -> Dict[str, Any]:
        paging: Dict[str, Any] = {"page_size": max(1, min(int(page_size), PAGE_SIZE_MAX))}
        if start_cursor:
            paging["start_cursor"] = start_cursor
        return paging

    # -- reads -----------------------------------------------------------
    def me(self) -> Dict[str, Any]:
        return self._request("GET", "users/me")

    def search(self, query: str, kind: str, page_size: int,
               start_cursor: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"sort": {"direction": "descending",
                                         "timestamp": "last_edited_time"},
                                **self._paging(page_size, start_cursor)}
        if query:
            body["query"] = query
        if kind in ("page", "database"):
            body["filter"] = {"property": "object", "value": kind}
        return self._request("POST", "search", json_body=body)

    def page(self, page_id: str) -> Dict[str, Any]:
        return self._request("GET", f"pages/{quoted(page_id)}")

    def children(self, block_id: str, page_size: int = PAGE_SIZE_MAX,
                 start_cursor: str = "") -> Dict[str, Any]:
        return self._request("GET", f"blocks/{quoted(block_id)}/children",
                             params=self._paging(page_size, start_cursor))

    def all_children(self, block_id: str, cap: int) -> Tuple[List[Dict[str, Any]], bool]:
        """The top-level blocks up to ``cap``, and whether there were more."""
        blocks: List[Dict[str, Any]] = []
        cursor = ""
        while True:
            answer = self.children(block_id, PAGE_SIZE_MAX, cursor)
            blocks.extend(answer.get("results") or [])
            if not answer.get("has_more"):
                return blocks[:cap], len(blocks) > cap
            if len(blocks) >= cap:
                return blocks[:cap], True
            cursor = str(answer.get("next_cursor") or "")

    def database(self, database_id: str) -> Dict[str, Any]:
        return self._request("GET", f"databases/{quoted(database_id)}")

    def query(self, database_id: str, page_size: int, start_cursor: str = "",
              filter_: Optional[Dict[str, Any]] = None,
              sorts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = self._paging(page_size, start_cursor)
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        return self._request("POST", f"databases/{quoted(database_id)}/query",
                             json_body=body)

    def comments(self, block_id: str, page_size: int, start_cursor: str = "") -> Dict[str, Any]:
        return self._request("GET", "comments",
                             params={"block_id": str(block_id).strip(),
                                     **self._paging(page_size, start_cursor)})

    # -- writes ----------------------------------------------------------
    def create_page(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "pages", json_body=body, write=True)

    def update_page(self, page_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", f"pages/{quoted(page_id)}",
                             json_body={"properties": properties}, write=True)

    def append(self, block_id: str, children: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._request("PATCH", f"blocks/{quoted(block_id)}/children",
                             json_body={"children": children}, write=True)

    def add_comment(self, page_id: str, rich_text: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._request("POST", "comments", write=True,
                             json_body={"parent": {"page_id": str(page_id).strip()},
                                        "rich_text": rich_text})
