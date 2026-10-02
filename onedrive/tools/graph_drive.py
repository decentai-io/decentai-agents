"""A small Microsoft Graph drive client over the connected account.

One class, no SDK, and the same failure kinds as the Outlook agent's
mail client:

- ``auth``: the account is not connected, or Microsoft no longer
  accepts its grant — reconnect it from the Credentials page.
- ``http``: Graph answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it.
- ``not_found``: OneDrive has no such file or folder.

Reads are retried once on a dropped connection; writes never are.

Items are addressed by id wherever Graph allows it, and by path only
to find a folder the user named. A file someone else shared lives in
their drive, so it is addressed with that drive's id as well. Ids and
names are escaped before they go into a path; a drive id carries a "!".
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
TRANSFER_TIMEOUT = 90


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def quoted(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


def item_path(item_id: str, drive_id: str = "") -> str:
    base = f"drives/{quoted(drive_id)}/items/" if drive_id else "me/drive/items/"
    return base + quoted(item_id)


def clean_path(path: str) -> str:
    """ "/Contracts//2026/" as "/Contracts/2026"; the top level is "/"."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p.strip()]
    return "/" + "/".join(parts)


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
                 data: Optional[bytes] = None, write: bool = False,
                 absolute: str = "", binary: bool = False, timeout: int = 0):
        url = absolute or f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = timeout or (WRITE_TIMEOUT if write else READ_TIMEOUT)
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            if data is not None:
                headers["Content-Type"] = "application/octet-stream"
            try:
                response = requests.request(
                    method, url, params=params, json=json, data=data,
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
                raise GraphError("not_found", "OneDrive has no such file or folder.")
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
        return self._request("GET", "me", params={"$select": "mail,userPrincipalName"})

    def drive(self) -> Dict[str, Any]:
        return self._request("GET", "me/drive")

    def item(self, item_id: str, drive_id: str = "") -> Dict[str, Any]:
        return self._request("GET", item_path(item_id, drive_id))

    def item_by_path(self, path: str) -> Dict[str, Any]:
        path = clean_path(path)
        if path == "/":
            return self._request("GET", "me/drive/root")
        return self._request("GET", "me/drive/root:" + urllib.parse.quote(path, safe="/"))

    def children(self, folder_id: str, top: int, page_link: str = "") -> Dict[str, Any]:
        """Graph pages with a full next-link URL, which is the page token."""
        if page_link:
            return self._request("GET", "", absolute=page_link)
        return self._request("GET", item_path(folder_id) + "/children", params={"$top": top})

    def search(self, query: str, top: int, page_link: str = "") -> Dict[str, Any]:
        if page_link:
            return self._request("GET", "", absolute=page_link)
        escaped = quoted(str(query).replace("'", "''"))
        return self._request("GET", f"me/drive/root/search(q='{escaped}')", params={"$top": top})

    def shared_with_me(self) -> List[Dict[str, Any]]:
        return self._request("GET", "me/drive/sharedWithMe").get("value") or []

    def content(self, item_id: str, drive_id: str = "", as_pdf: bool = False) -> bytes:
        """The bytes. Graph answers with a redirect to a pre-signed
        address, which requests follows."""
        return self._request("GET", item_path(item_id, drive_id) + "/content",
                             params={"format": "pdf"} if as_pdf else None,
                             binary=True, timeout=TRANSFER_TIMEOUT)

    def permissions(self, item_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", item_path(item_id) + "/permissions").get("value") or []

    # -- writes ----------------------------------------------------------
    def upload(self, parent_id: str, name: str, raw: bytes, on_conflict: str) -> Dict[str, Any]:
        """One request, for files within Graph's simple-upload limit."""
        return self._request(
            "PUT", f"{item_path(parent_id)}:/{quoted(name)}:/content",
            params={"@microsoft.graph.conflictBehavior": on_conflict},
            data=raw, write=True, timeout=TRANSFER_TIMEOUT)

    def create_folder(self, parent_id: str, name: str) -> Dict[str, Any]:
        return self._request("POST", item_path(parent_id) + "/children", write=True, json={
            "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def update(self, item_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", item_path(item_id), json=patch, write=True)

    def delete(self, item_id: str) -> None:
        """To the recycle bin, not gone."""
        self._request("DELETE", item_path(item_id), write=True)

    def invite(self, item_id: str, recipients: List[str], role: str,
               message: str = "") -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"recipients": [{"email": r} for r in recipients],
                                "roles": [role], "requireSignIn": True,
                                "sendInvitation": True}
        if message:
            body["message"] = message
        return self._request("POST", item_path(item_id) + "/invite",
                             json=body, write=True).get("value") or []

    def create_link(self, item_id: str, link_type: str, scope: str) -> Dict[str, Any]:
        return self._request("POST", item_path(item_id) + "/createLink", write=True,
                             json={"type": link_type, "scope": scope})

    def delete_permission(self, item_id: str, permission_id: str) -> None:
        self._request("DELETE", f"{item_path(item_id)}/permissions/{quoted(permission_id)}",
                      write=True)


def _person(identity: Any) -> str:
    user = (identity or {}).get("user") or (identity or {}).get("siteUser") or {}
    return str(user.get("email") or user.get("displayName") or "")


def item_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """A drive item as the agent shows it. A shared-with-me entry wraps
    the real item in remoteItem, which lives in another drive."""
    remote = item.get("remoteItem")
    source = remote or item
    parent = source.get("parentReference") or {}
    path = str(parent.get("path") or "")
    row = {
        "item_id": str(source.get("id") or item.get("id") or ""),
        "name": str(source.get("name") or item.get("name") or ""),
        "kind": "folder" if "folder" in source else "file",
        "size": int(source.get("size") or 0),
        "mime_type": str((source.get("file") or {}).get("mimeType") or ""),
        "modified": str(source.get("lastModifiedDateTime") or ""),
        "modified_by": _person(source.get("lastModifiedBy")),
        "folder": (path.split("root:", 1)[1] or "/") if "root:" in path else "",
        "link": str(source.get("webUrl") or ""),
        "shared": "shared" in source,
    }
    if remote:
        row["drive_id"] = str(parent.get("driveId") or "")
        row["shared_by"] = _person((remote.get("shared") or {}).get("sharedBy"))
    return row


def permission_row(permission: Dict[str, Any]) -> Dict[str, Any]:
    roles = permission.get("roles") or []
    role = "owner" if "owner" in roles else "write" if "write" in roles else "read"
    link = permission.get("link") or {}
    if link:
        who = {"anonymous": "anyone with the link",
               "organization": "people in your organization"}.get(
            str(link.get("scope") or ""), "specific people with the link")
        via = "link"
    else:
        who = _person(permission.get("grantedToV2") or permission.get("grantedTo"))
        via = "owner" if role == "owner" else "invite"
    if permission.get("inheritedFrom"):
        via = "inherited"
    return {"permission_id": str(permission.get("id") or ""), "who": who,
            "role": role, "via": via, "link": str(link.get("webUrl") or "")}
