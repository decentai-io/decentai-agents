"""A small Box (API 2.0) client over the connected account.

The same shape as the other drive agents' clients, and the same failure
kinds:

- ``auth``: the account is not connected, or Box no longer accepts its
  token — reconnect it from the Credentials page.
- ``http``: Box answered with an error — the message says what. A name
  already taken (409) names the item it clashes with.
- ``unknown``: a write was sent and no answer came back. The outcome is
  unknown; nothing retries it.
- ``not_found``: Box has no such file or folder.
- ``rate_limited``: Box asked the account to slow down.

Reads are retried once on a dropped connection; writes never are.

Three Box specifics. Files and folders are separate kinds with separate
addresses (/files/{id}, /folders/{id}), and the top folder is "0".
Uploads go to a different host from everything else. And a download
answers with a redirect to a short-lived address, which requests
follows — dropping the token on the way when the host changes, as it
should.
"""

from __future__ import annotations

import json
import mimetypes
from typing import Any, Dict, List, Optional, Tuple

import requests

API_BASE_URL = "https://api.box.com/2.0"
UPLOAD_BASE_URL = "https://upload.box.com/api/2.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
TRANSFER_TIMEOUT = 90

ROOT = "0"
FIELDS = ("type,id,name,size,modified_at,modified_by,parent,path_collection,"
          "shared_link,owned_by,item_status")
COLLABORATION_FIELDS = "id,role,status,accessible_by,invite_email,item"


class BoxError(Exception):
    def __init__(self, kind: str, message: str, code: str = "",
                 conflict: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found" | "rate_limited"
        self.message = message
        self.code = code          # Box's own code, e.g. "item_name_in_use"
        self.conflict = conflict or {}


class BoxClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token (Box issues a new one each time), and hands this
        # agent an access token that is still good at the moment of the
        # call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        override = str(secret.get("api_base_url") or "").rstrip("/")
        # In tests one loopback address stands in for both hosts; the
        # upload paths do not collide with the API's.
        self.api_base_url = override or API_BASE_URL
        self.upload_base_url = override or UPLOAD_BASE_URL

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise BoxError("auth", "The Box account is not connected — connect it "
                                   "from the agent's Credentials tab.")
        return self.access_token

    @staticmethod
    def _refusal(response: requests.Response, method: str, path: str) -> BoxError:
        status = response.status_code
        if status == 401:
            return BoxError("auth", "The Box connection has expired or was revoked — "
                                    "reconnect the account from its Credentials page.")
        if status == 404:
            return BoxError("not_found", "Box has no such file or folder.")
        if status == 429:
            wait = response.headers.get("Retry-After") or "a little while"
            return BoxError("rate_limited", f"Box asked to slow down; try again in "
                                            f"{wait} seconds.")
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        code = str(body.get("code") or "")
        if status == 409:
            conflicts = (body.get("context_info") or {}).get("conflicts") or {}
            # A single conflict comes as an object, several as a list.
            conflict = conflicts[0] if isinstance(conflicts, list) and conflicts else \
                conflicts if isinstance(conflicts, dict) else {}
            name = str(conflict.get("name") or "")
            return BoxError("http", f"Box already has an item named '{name}' there."
                            if name else f"Box refused {method} {path}: "
                                         f"{body.get('message') or code}",
                            code, conflict)
        detail = str(body.get("message") or code or (response.text or "").strip())[:300]
        return BoxError("http", f"Box refused {method} {path}: {detail}", code)

    # -- one request -----------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 files=None, write: bool = False, binary: bool = False,
                 upload: bool = False, absolute: str = "", headers=None,
                 timeout: int = 0) -> Any:
        base = self.upload_base_url if upload else self.api_base_url
        url = absolute or f"{base}/{path.lstrip('/')}"
        timeout = timeout or (WRITE_TIMEOUT if write else READ_TIMEOUT)
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                response = requests.request(
                    method, url, params=params, json=json_body, files=files,
                    headers={"Authorization": f"Bearer {self._token()}", **(headers or {})},
                    timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise BoxError("unknown", f"No answer from Box for {method} {path}: "
                                              f"the outcome is unknown ({exc}).")
                continue
            if response.status_code >= 400:
                raise self._refusal(response, method, path)
            if binary:
                return response.content
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise BoxError("http", f"Box could not be reached: {last}")

    # -- account ---------------------------------------------------------
    def me(self) -> Dict[str, Any]:
        return self._request("GET", "users/me", params={
            "fields": "login,name,space_amount,space_used,enterprise"})

    # -- reads -----------------------------------------------------------
    def file(self, file_id: str) -> Dict[str, Any]:
        return self._request("GET", f"files/{file_id}", params={"fields": FIELDS})

    def folder(self, folder_id: str) -> Dict[str, Any]:
        return self._request("GET", f"folders/{folder_id}", params={"fields": FIELDS})

    def item(self, item_id: str) -> Dict[str, Any]:
        """A file, or failing that a folder, by id. Box addresses the two
        kinds separately; a result row says which it is, but a person
        may hand over a bare id."""
        try:
            return self.file(item_id)
        except BoxError as exc:
            if exc.kind != "not_found":
                raise
        return self.folder(item_id)

    def items(self, folder_id: str, limit: int, offset: int = 0) -> Dict[str, Any]:
        """Box sorts a folder's folders before its files, then by name."""
        return self._request("GET", f"folders/{folder_id}/items", params={
            "fields": FIELDS, "limit": limit, "offset": offset,
            "sort": "name", "direction": "ASC"})

    def search(self, query: str, limit: int, offset: int = 0) -> Dict[str, Any]:
        return self._request("GET", "search", params={
            "query": query, "type": "file", "limit": limit, "offset": offset,
            "fields": FIELDS})

    def content(self, file_id: str) -> bytes:
        return self._request("GET", f"files/{file_id}/content", binary=True,
                             timeout=TRANSFER_TIMEOUT)

    def pdf_representation(self, file_id: str) -> Dict[str, Any]:
        """Box's own PDF rendering of a document, asked for by hint."""
        answer = self._request("GET", f"files/{file_id}", params={"fields": "representations"},
                               headers={"X-Rep-Hints": "[pdf]"})
        entries = (answer.get("representations") or {}).get("entries") or []
        return next((e for e in entries if e.get("representation") == "pdf"), {})

    def fetch_json(self, url: str) -> Dict[str, Any]:
        return self._request("GET", url, absolute=url)

    def fetch(self, url: str) -> bytes:
        return self._request("GET", url, absolute=url, binary=True, timeout=TRANSFER_TIMEOUT)

    def preflight(self, parent_id: str, name: str, size: int) -> None:
        """Would an upload of this name be accepted? A 409 says no and
        names the file already there. Nothing is written."""
        self._request("OPTIONS", "files/content", json_body={
            "name": name, "parent": {"id": parent_id}, "size": size})

    def collaborations(self, kind: str, item_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"{kind}s/{item_id}/collaborations", params={
            "fields": COLLABORATION_FIELDS}).get("entries") or []

    def events(self, position: str, limit: int) -> Dict[str, Any]:
        return self._request("GET", "events", params={
            "stream_type": "changes", "stream_position": position, "limit": limit})

    # -- writes ----------------------------------------------------------
    def upload(self, parent_id: str, name: str, raw: bytes) -> Dict[str, Any]:
        """One multipart request; the attributes part must come first."""
        answer = self._request("POST", "files/content", upload=True, write=True, files={
            "attributes": (None, json.dumps({"name": name, "parent": {"id": parent_id}}),
                           "application/json"),
            "file": (name, raw, mimetypes.guess_type(name)[0] or "application/octet-stream")},
            params={"fields": FIELDS}, timeout=TRANSFER_TIMEOUT)
        return (answer.get("entries") or [{}])[0]

    def upload_version(self, file_id: str, name: str, raw: bytes) -> Dict[str, Any]:
        """New bytes for the same file; Box keeps the old as a version."""
        answer = self._request("POST", f"files/{file_id}/content", upload=True, write=True,
                               files={"attributes": (None, json.dumps({"name": name}),
                                                     "application/json"),
                                      "file": (name, raw, "application/octet-stream")},
                               params={"fields": FIELDS}, timeout=TRANSFER_TIMEOUT)
        return (answer.get("entries") or [{}])[0]

    def create_folder(self, parent_id: str, name: str) -> Dict[str, Any]:
        return self._request("POST", "folders", params={"fields": FIELDS}, write=True,
                             json_body={"name": name, "parent": {"id": parent_id}})

    def update(self, kind: str, item_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"{kind}s/{item_id}", params={"fields": FIELDS},
                             json_body=body, write=True)

    def trash(self, kind: str, item_id: str) -> None:
        """To the trash. A folder goes with what is in it."""
        self._request("DELETE", f"{kind}s/{item_id}", write=True,
                      params={"recursive": "true"} if kind == "folder" else None)

    def collaborate(self, kind: str, item_id: str, email: str, role: str) -> Dict[str, Any]:
        return self._request("POST", "collaborations", params={"notify": "true"}, write=True,
                             json_body={"item": {"type": kind, "id": item_id},
                                        "accessible_by": {"type": "user", "login": email},
                                        "role": "editor" if role == "write" else "viewer"})

    def remove_collaboration(self, collaboration_id: str) -> None:
        self._request("DELETE", f"collaborations/{collaboration_id}", write=True)

    def set_link(self, kind: str, item_id: str, link: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """A shared link's settings, or None to remove the link."""
        return self._request("PUT", f"{kind}s/{item_id}", params={"fields": FIELDS},
                             json_body={"shared_link": link}, write=True)


def folder_path(item: Dict[str, Any]) -> str:
    """The folders above an item as a path; "All Files" is the top."""
    entries = (item.get("path_collection") or {}).get("entries") or []
    names = [str(e.get("name") or "") for e in entries if str(e.get("id")) != ROOT]
    return "/" + "/".join(names)


def item_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """A file or folder as the agent shows it. ``link`` is the shared
    link where there is one; nothing else is a real address to give."""
    kind = "folder" if item.get("type") == "folder" else "file"
    name = str(item.get("name") or "")
    folder = folder_path(item)
    link = item.get("shared_link") or {}
    return {
        "item_id": str(item.get("id") or ""),
        "name": name,
        "kind": kind,
        "size": int(item.get("size") or 0),
        # Box keeps no content type; the name's extension is all there is.
        "mime_type": "" if kind == "folder" else (mimetypes.guess_type(name)[0] or ""),
        "modified": str(item.get("modified_at") or ""),
        "modified_by": str((item.get("modified_by") or {}).get("login") or ""),
        "folder": folder if item.get("path_collection") else "",
        "path": (folder.rstrip("/") + "/" + name) if item.get("path_collection") else "",
        "link": str(link.get("url") or ""),
        "shared": bool(link),
    }


ROLES = {"editor": "write", "co-owner": "write", "viewer": "read", "previewer": "read",
         "uploader": "read", "previewer uploader": "read", "viewer uploader": "read"}
REACH = {"open": "anyone with the link", "company": "people in your company",
         "collaborators": "the people already invited"}


def access_rows(item: Dict[str, Any], collaborations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The owner, each collaboration, and the shared link, as permission
    rows. A collaboration on a folder above the item is inherited; its id
    is Box's collaboration id, and the link's is "link"."""
    owner = item.get("owned_by") or {}
    rows = [{"permission_id": "owner", "who": str(owner.get("login") or owner.get("name") or ""),
             "role": "owner", "via": "owner", "link": ""}]
    for collaboration in collaborations:
        person = collaboration.get("accessible_by") or {}
        where = str((collaboration.get("item") or {}).get("id") or "")
        rows.append({
            "permission_id": str(collaboration.get("id") or ""),
            "who": str(person.get("login") or collaboration.get("invite_email")
                       or person.get("name") or ""),
            "role": ROLES.get(str(collaboration.get("role") or ""), "read"),
            "via": "inherited" if where and where != str(item.get("id")) else "invite",
            "link": ""})
    link = item.get("shared_link") or {}
    if link:
        rows.append({"permission_id": "link",
                     "who": REACH.get(str(link.get("access") or ""), str(link.get("access") or "")),
                     "role": "write" if (link.get("permissions") or {}).get("can_edit") else "read",
                     "via": "link", "link": str(link.get("url") or "")})
    return rows


def kind_of(item: Dict[str, Any]) -> str:
    return "folder" if item.get("type") == "folder" else "file"


def split_token(token: str) -> Tuple[str, int]:
    """A folder listing's page token: "<folder id>:<offset>"."""
    folder_id, _, offset = str(token).partition(":")
    return folder_id, int(offset or 0)
