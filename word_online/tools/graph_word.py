"""A small Microsoft Graph client for Word files in OneDrive.

Graph has no API for what is inside a Word document — no paragraphs,
no comments. What it has is the file: this client finds ``.docx`` items,
reads their metadata (``eTag``, last modified, by whom), downloads their
bytes, and uploads new bytes with ``If-Match`` so a save that raced
another is refused (412) instead of overwriting it. Everything about
the content is read from the package itself (see docx_file.py).

The same failure kinds as the OneDrive agent's client — ``auth``,
``http``, ``unknown`` (a write got no answer; never retried),
``not_found`` — plus ``conflict``: the file's eTag no longer matched.
Reads are retried once on a dropped connection; writes never are.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 60
TRANSFER_TIMEOUT = 90

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ITEM_SELECT = "id,name,size,eTag,lastModifiedDateTime,lastModifiedBy,webUrl,file,parentReference"


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found" | "conflict"
        self.message = message


def quoted(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


class WordClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        self.api_base_url = (str(secret.get("api_base_url") or "")
                             or API_BASE_URL).rstrip("/")

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

    def _request(self, method: str, path: str, *, params=None, data: Optional[bytes] = None,
                 headers: Optional[Dict[str, str]] = None, write: bool = False,
                 binary: bool = False, timeout: int = 0):
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = timeout or (WRITE_TIMEOUT if write else READ_TIMEOUT)
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            sent = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
            try:
                response = requests.request(method, url, params=params, data=data,
                                            headers=sent, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GraphError("unknown", f"No answer from Microsoft for {method} "
                                                f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GraphError("auth", "The Microsoft connection has expired "
                                         "or was revoked — reconnect the "
                                         "account from its Credentials page.")
            if response.status_code == 404:
                raise GraphError("not_found", "OneDrive has no such file.")
            if response.status_code == 412:
                raise GraphError("conflict", "The file was saved by someone else in "
                                             "the meantime; OneDrive refused to "
                                             "overwrite it.")
            if response.status_code >= 400:
                raise GraphError("http", f"Microsoft refused {method} {path}: "
                                         f"{self._detail(response)}")
            if binary:
                return response.content
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise GraphError("http", f"Microsoft could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "me", params={"$select": "mail,userPrincipalName,displayName"})

    def search(self, query: str, top: int) -> List[Dict[str, Any]]:
        escaped = quoted(str(query).replace("'", "''"))
        return self._request("GET", f"me/drive/root/search(q='{escaped}')",
                             params={"$top": top}).get("value") or []

    def item(self, item_id: str) -> Dict[str, Any]:
        return self._request("GET", "me/drive/items/" + quoted(item_id),
                             params={"$select": ITEM_SELECT})

    def content(self, item_id: str) -> bytes:
        """The bytes. Graph answers with a redirect to a pre-signed
        address, which requests follows."""
        return self._request("GET", f"me/drive/items/{quoted(item_id)}/content",
                             binary=True, timeout=TRANSFER_TIMEOUT)

    # -- the one write ---------------------------------------------------
    def replace_content(self, item_id: str, raw: bytes, etag: str) -> Dict[str, Any]:
        """New bytes for the same item, only if it is still at ``etag``."""
        return self._request("PUT", f"me/drive/items/{quoted(item_id)}/content", data=raw,
                             headers={"Content-Type": DOCX, "If-Match": etag},
                             write=True, timeout=TRANSFER_TIMEOUT)


def person(identity: Any) -> str:
    user = (identity or {}).get("user") or {}
    return str(user.get("email") or user.get("displayName") or "")


def is_docx(item: Dict[str, Any]) -> bool:
    return "file" in item and str(item.get("name") or "").lower().endswith(".docx")
