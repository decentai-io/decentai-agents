"""A small Google Drive (v3) client over the connected account.

The same shape as the Gmail and Google Calendar agents' clients, and
the same failure kinds — ``auth`` (reconnect), ``http`` (Google
refused), ``unknown`` (a write got no answer; never retried) — plus
``not_found``. Reads are retried once on a dropped connection; writes
never are.

Three Drive specifics. Drive has no paths, only parents, so a folder
the user names is found one segment at a time. A Google Doc, Sheet or
Slides file has no bytes of its own and is exported instead. And a
name is not unique in a folder, so "does this name exist here" is a
question the agent asks before it uploads, not one Drive answers.
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://www.googleapis.com"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
TRANSFER_TIMEOUT = 90

FOLDER = "application/vnd.google-apps.folder"
FIELDS = ("id,name,mimeType,size,modifiedTime,parents,webViewLink,shared,trashed,"
          "lastModifyingUser(emailAddress,displayName),sharingUser(emailAddress,displayName)")
PERMISSION_FIELDS = "permissions(id,type,role,emailAddress,domain,displayName,permissionDetails)"

# What a Google-native file becomes when it is brought out of Drive.
EXPORTS = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


class GoogleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def literal(text: str) -> str:
    """A value inside a Drive query: quoted, with quote and backslash escaped."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def file_path(item_id: str) -> str:
    return "drive/v3/files/" + urllib.parse.quote(str(item_id), safe="")


class DriveClient:
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
            return str(error.get("message") or error.get("status") or error)[:300]
        return str(error or body)[:300]

    # -- one request -----------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 data: Optional[bytes] = None, content_type: str = "",
                 write: bool = False, binary: bool = False, timeout: int = 0):
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = timeout or (WRITE_TIMEOUT if write else READ_TIMEOUT)
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                response = requests.request(
                    method, url, params=params, json=json_body, data=data,
                    headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GoogleError(
                        "unknown", f"No answer from Google Drive for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GoogleError("auth", "The Google connection has expired or "
                                          "was revoked — reconnect the account "
                                          "from its Credentials page.")
            if response.status_code == 404:
                raise GoogleError("not_found", "Google Drive has no such file or folder.")
            if response.status_code >= 400:
                raise GoogleError("http", f"Google Drive refused {method} {path}: "
                                          f"{self._detail(response)}")
            if binary:
                return response.content
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GoogleError("http", f"Google Drive could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def about(self) -> Dict[str, Any]:
        return self._request("GET", "drive/v3/about",
                             params={"fields": "user(emailAddress),storageQuota(limit,usage)"})

    def files(self, query: str, page_size: int, page_token: str = "",
              order_by: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"q": query, "pageSize": page_size,
                                  "fields": f"nextPageToken,files({FIELDS})"}
        if page_token:
            params["pageToken"] = page_token
        if order_by:
            params["orderBy"] = order_by
        return self._request("GET", "drive/v3/files", params=params)

    def item(self, item_id: str) -> Dict[str, Any]:
        return self._request("GET", file_path(item_id), params={"fields": FIELDS})

    def in_folder(self, parent_id: str, name: str = "", folders_only: bool = False) -> List[Dict[str, Any]]:
        clauses = [f"{literal(parent_id)} in parents", "trashed = false"]
        if name:
            clauses.insert(1, f"name = {literal(name)}")
        if folders_only:
            clauses.insert(-1, f"mimeType = '{FOLDER}'")
        return self.files(" and ".join(clauses), 1000).get("files") or []

    def media(self, item_id: str) -> bytes:
        return self._request("GET", file_path(item_id), params={"alt": "media"},
                             binary=True, timeout=TRANSFER_TIMEOUT)

    def export(self, item_id: str, mime_type: str) -> bytes:
        return self._request("GET", file_path(item_id) + "/export",
                             params={"mimeType": mime_type}, binary=True,
                             timeout=TRANSFER_TIMEOUT)

    def permissions(self, item_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", file_path(item_id) + "/permissions",
                             params={"fields": PERMISSION_FIELDS}).get("permissions") or []

    # -- writes ----------------------------------------------------------
    def upload(self, parent_id: str, name: str, raw: bytes, mime_type: str) -> Dict[str, Any]:
        """One multipart request: the metadata, then the bytes."""
        boundary = "decentai-" + uuid.uuid4().hex
        metadata = json.dumps({"name": name, "parents": [parent_id]}).encode("utf-8")
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + metadata
                + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
                + raw + f"\r\n--{boundary}--".encode())
        return self._request("POST", "upload/drive/v3/files",
                             params={"uploadType": "multipart", "fields": FIELDS},
                             data=body, content_type=f"multipart/related; boundary={boundary}",
                             write=True, timeout=TRANSFER_TIMEOUT)

    def replace(self, item_id: str, raw: bytes, mime_type: str) -> Dict[str, Any]:
        """New bytes for the same file; Drive keeps the old as a version."""
        return self._request("PATCH", "upload/" + file_path(item_id),
                             params={"uploadType": "media", "fields": FIELDS},
                             data=raw, content_type=mime_type, write=True,
                             timeout=TRANSFER_TIMEOUT)

    def create_folder(self, parent_id: str, name: str) -> Dict[str, Any]:
        return self._request("POST", "drive/v3/files", params={"fields": FIELDS}, write=True,
                             json_body={"name": name, "mimeType": FOLDER, "parents": [parent_id]})

    def update(self, item_id: str, body: Dict[str, Any], add_parents: str = "",
               remove_parents: str = "") -> Dict[str, Any]:
        params = {"fields": FIELDS}
        if add_parents:
            params["addParents"] = add_parents
        if remove_parents:
            params["removeParents"] = remove_parents
        return self._request("PATCH", file_path(item_id), params=params,
                             json_body=body, write=True)

    def trash(self, item_id: str) -> Dict[str, Any]:
        """To the trash. Nothing here deletes for good."""
        return self.update(item_id, {"trashed": True})

    def add_permission(self, item_id: str, body: Dict[str, Any], notify: bool,
                       message: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"sendNotificationEmail": "true" if notify else "false"}
        if message and notify:
            params["emailMessage"] = message
        return self._request("POST", file_path(item_id) + "/permissions",
                             params=params, json_body=body, write=True)

    def delete_permission(self, item_id: str, permission_id: str) -> None:
        self._request("DELETE", f"{file_path(item_id)}/permissions/"
                                f"{urllib.parse.quote(str(permission_id), safe='')}", write=True)


def _person(user: Any) -> str:
    return str((user or {}).get("emailAddress") or (user or {}).get("displayName") or "")


def item_row(item: Dict[str, Any], folder: str = "") -> Dict[str, Any]:
    row = {
        "item_id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "kind": "folder" if item.get("mimeType") == FOLDER else "file",
        "size": int(item.get("size") or 0),
        "mime_type": str(item.get("mimeType") or ""),
        "modified": str(item.get("modifiedTime") or ""),
        "modified_by": _person(item.get("lastModifyingUser")),
        "folder": folder,
        "link": str(item.get("webViewLink") or ""),
        "shared": bool(item.get("shared")),
    }
    if item.get("sharingUser"):
        row["shared_by"] = _person(item["sharingUser"])
    return row


ROLES = {"owner": "owner", "organizer": "write", "fileOrganizer": "write",
         "writer": "write", "commenter": "comment", "reader": "read"}


def permission_row(permission: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(permission.get("type") or "")
    role = ROLES.get(str(permission.get("role") or ""), "read")
    if kind == "anyone":
        who, via = "anyone with the link", "link"
    elif kind == "domain":
        who, via = f"people at {permission.get('domain') or 'your organization'}", "link"
    else:
        who = str(permission.get("emailAddress") or permission.get("displayName") or "")
        via = "owner" if role == "owner" else "invite"
    details = permission.get("permissionDetails") or []
    if details and all(d.get("inherited") for d in details):
        via = "inherited"
    return {"permission_id": str(permission.get("id") or ""), "who": who,
            "role": role, "via": via, "link": ""}
