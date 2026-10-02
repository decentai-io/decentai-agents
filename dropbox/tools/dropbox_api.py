"""A small Dropbox (API v2) client over the connected account.

The same shape as the Google Drive and OneDrive agents' clients, and the
same failure kinds:

- ``auth``: the account is not connected, or Dropbox no longer accepts
  its token — reconnect it from the Credentials page.
- ``http``: Dropbox answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome is
  unknown; nothing retries it.
- ``not_found``: Dropbox has no such file or folder.
- ``rate_limited``: Dropbox asked the account to slow down.

Reads are retried once on a dropped connection; writes never are.

Three Dropbox specifics. Every call is a POST, so whether a call is a
read is said by the caller, not the verb. An RPC call carries its
arguments as a JSON body; a content call (download, upload) carries
them in the ``Dropbox-API-Arg`` header, which must be pure ASCII, and
answers with its metadata in ``Dropbox-API-Result``. And an error is a
409 whose ``error_summary`` reads like a path — ``path/not_found/..`` —
which is what the agent matches on.
"""

from __future__ import annotations

import json
import mimetypes
from typing import Any, Dict, List, Optional, Tuple

import requests

API_BASE_URL = "https://api.dropboxapi.com"
CONTENT_BASE_URL = "https://content.dropboxapi.com"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
TRANSFER_TIMEOUT = 90


class DropboxError(Exception):
    def __init__(self, kind: str, message: str, summary: str = "", body: Any = None):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found" | "rate_limited"
        self.message = message
        self.summary = summary    # Dropbox's error_summary, e.g. "reset/.."
        self.body = body or {}


def header_json(value: Dict[str, Any]) -> str:
    """JSON for the Dropbox-API-Arg header: every non-ASCII character,
    and DEL, escaped as \\uXXXX, because an HTTP header cannot carry
    them and Dropbox documents exactly this escaping."""
    return json.dumps(value, ensure_ascii=True).replace("\x7f", "\\u007f")


def clean_path(path: str) -> str:
    """ "/Contracts//2026/" as "/Contracts/2026"; the top level is "/"."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p.strip()]
    return "/" + "/".join(parts)


def api_path(path: str) -> str:
    """What Dropbox calls the top level is the empty string, not "/"."""
    path = clean_path(path)
    return "" if path == "/" else path


def address(item_id_or_path: str) -> str:
    """An item as the user gave it: an id ("id:…") or a path."""
    text = str(item_id_or_path or "").strip()
    if text.startswith(("id:", "rev:", "ns:")):
        return text
    return api_path(text)


class DropboxClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        override = str(secret.get("api_base_url") or "").rstrip("/")
        # In tests one loopback address stands in for both hosts; their
        # method paths do not collide.
        self.api_base_url = override or API_BASE_URL
        self.content_base_url = override or CONTENT_BASE_URL
        self._people: Dict[str, str] = {}

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise DropboxError("auth", "The Dropbox account is not connected — "
                                       "connect it from the agent's Credentials "
                                       "tab.")
        return self.access_token

    @staticmethod
    def _refusal(response: requests.Response, method: str) -> DropboxError:
        if response.status_code == 401:
            return DropboxError("auth", "The Dropbox connection has expired or was "
                                        "revoked — reconnect the account from its "
                                        "Credentials page.")
        if response.status_code == 429:
            wait = response.headers.get("Retry-After") or "a little while"
            return DropboxError("rate_limited", f"Dropbox asked to slow down; try "
                                                f"again in {wait} seconds.")
        try:
            body = response.json()
        except ValueError:
            body = {}
        summary = str(body.get("error_summary") or "") if isinstance(body, dict) else ""
        detail = summary or (response.text or "").strip()[:300]
        if response.status_code == 409 and "not_found" in summary:
            return DropboxError("not_found", "Dropbox has no such file or folder.",
                                summary, body)
        return DropboxError("http", f"Dropbox refused {method}: {detail}", summary, body)

    # -- one request -----------------------------------------------------
    def _send(self, url: str, method: str, *, data: bytes, headers: Dict[str, str],
              write: bool, timeout: int) -> requests.Response:
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                response = requests.post(url, data=data, timeout=timeout, headers={
                    "Authorization": f"Bearer {self._token()}", **headers})
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise DropboxError(
                        "unknown", f"No answer from Dropbox for {method}: the "
                                   f"outcome is unknown ({exc}).")
                continue
            if response.status_code >= 400:
                raise self._refusal(response, method)
            return response
        raise DropboxError("http", f"Dropbox could not be reached: {last}")

    def rpc(self, method: str, arguments: Optional[Dict[str, Any]] = None, *,
            write: bool = False) -> Any:
        """An RPC call. No arguments is the JSON body null, not an empty body."""
        response = self._send(
            f"{self.api_base_url}/2/{method}", method,
            data=json.dumps(arguments).encode("utf-8"),
            headers={"Content-Type": "application/json"}, write=write,
            timeout=WRITE_TIMEOUT if write else READ_TIMEOUT)
        if not response.content:
            return {}
        answer = response.json()
        return {} if answer is None else answer

    def content_down(self, method: str, arguments: Dict[str, Any]) -> Tuple[Dict[str, Any], bytes]:
        """A content download: the metadata from the header, the bytes as the body."""
        response = self._send(
            f"{self.content_base_url}/2/{method}", method, data=b"",
            headers={"Dropbox-API-Arg": header_json(arguments)}, write=False,
            timeout=TRANSFER_TIMEOUT)
        result = response.headers.get("Dropbox-API-Result") or "{}"
        return json.loads(result), response.content

    def content_up(self, method: str, arguments: Dict[str, Any], raw: bytes) -> Dict[str, Any]:
        response = self._send(
            f"{self.content_base_url}/2/{method}", method, data=raw,
            headers={"Dropbox-API-Arg": header_json(arguments),
                     "Content-Type": "application/octet-stream"},
            write=True, timeout=TRANSFER_TIMEOUT)
        return response.json() if response.content else {}

    # -- account ---------------------------------------------------------
    def account(self) -> Dict[str, Any]:
        return self.rpc("users/get_current_account")

    def space(self) -> Dict[str, Any]:
        return self.rpc("users/get_space_usage")

    def person(self, account_id: str) -> str:
        """An account id as an address, once per id. Dropbox names who
        changed a file in a shared folder only by account id."""
        if not account_id:
            return ""
        if account_id not in self._people:
            try:
                found = self.rpc("users/get_account", {"account_id": account_id})
                self._people[account_id] = str(found.get("email") or
                                               (found.get("name") or {}).get("display_name") or "")
            except DropboxError:
                self._people[account_id] = ""
        return self._people[account_id]

    # -- reads -----------------------------------------------------------
    def metadata(self, item: str) -> Dict[str, Any]:
        return self.rpc("files/get_metadata", {"path": address(item),
                                               "include_has_explicit_shared_members": True})

    def list_folder(self, path: str, limit: int) -> Dict[str, Any]:
        return self.rpc("files/list_folder", {"path": address(path), "limit": limit,
                                              "include_has_explicit_shared_members": True})

    def list_folder_continue(self, cursor: str) -> Dict[str, Any]:
        return self.rpc("files/list_folder/continue", {"cursor": cursor})

    def latest_cursor(self, path: str, limit: int) -> str:
        answer = self.rpc("files/list_folder/get_latest_cursor", {
            "path": address(path), "recursive": True, "limit": limit})
        return str(answer.get("cursor") or "")

    def search(self, query: str, limit: int, cursor: str = "") -> Dict[str, Any]:
        if cursor:
            return self.rpc("files/search/continue_v2", {"cursor": cursor})
        return self.rpc("files/search_v2", {"query": query, "options": {
            "max_results": limit, "file_status": "active", "filename_only": False}})

    def received_files(self, limit: int) -> List[Dict[str, Any]]:
        return self.rpc("sharing/list_received_files", {"limit": limit}).get("entries") or []

    def shared_folders(self, limit: int) -> List[Dict[str, Any]]:
        return self.rpc("sharing/list_folders", {"limit": limit}).get("entries") or []

    def received_file(self, file_id: str) -> Dict[str, Any]:
        return self.rpc("sharing/get_file_metadata", {"file": file_id})

    def link_metadata(self, url: str) -> Dict[str, Any]:
        return self.rpc("sharing/get_shared_link_metadata", {"url": url})

    def download(self, item: str) -> Tuple[Dict[str, Any], bytes]:
        return self.content_down("files/download", {"path": address(item)})

    def export(self, item: str) -> Tuple[Dict[str, Any], bytes]:
        return self.content_down("files/export", {"path": address(item)})

    def preview(self, item: str) -> bytes:
        """Dropbox's own PDF rendering of a Word or PowerPoint file."""
        return self.content_down("files/get_preview", {"path": address(item)})[1]

    def link_file(self, url: str) -> bytes:
        return self.content_down("sharing/get_shared_link_file", {"url": url})[1]

    def file_members(self, item: str) -> Dict[str, Any]:
        return self.rpc("sharing/list_file_members", {
            "file": address(item), "include_inherited": True, "limit": 100})

    def folder_members(self, shared_folder_id: str) -> Dict[str, Any]:
        return self.rpc("sharing/list_folder_members", {
            "shared_folder_id": shared_folder_id, "limit": 100})

    def links(self, item: str) -> List[Dict[str, Any]]:
        return self.rpc("sharing/list_shared_links", {
            "path": address(item), "direct_only": True}).get("links") or []

    def share_job(self, job_id: str) -> Dict[str, Any]:
        return self.rpc("sharing/check_share_job_status", {"async_job_id": job_id})

    # -- writes ----------------------------------------------------------
    def upload(self, path: str, raw: bytes, on_conflict: str) -> Dict[str, Any]:
        """One request, within Dropbox's 150 MB single-upload limit.
        "rename" is Dropbox's own autorename, done atomically on its side."""
        return self.content_up("files/upload", {
            "path": path, "mode": "overwrite" if on_conflict == "replace" else "add",
            "autorename": on_conflict == "rename", "mute": False}, raw)

    def create_folder(self, path: str) -> Dict[str, Any]:
        return self.rpc("files/create_folder_v2", {"path": path, "autorename": False},
                        write=True).get("metadata") or {}

    def move(self, from_path: str, to_path: str) -> Dict[str, Any]:
        return self.rpc("files/move_v2", {"from_path": from_path, "to_path": to_path,
                                          "autorename": False}, write=True).get("metadata") or {}

    def delete(self, item: str) -> Dict[str, Any]:
        """To Dropbox's deleted files, restorable for the plan's retention."""
        return self.rpc("files/delete_v2", {"path": address(item)},
                        write=True).get("metadata") or {}

    def add_file_members(self, item: str, emails: List[str], role: str,
                         message: str) -> List[Dict[str, Any]]:
        arguments: Dict[str, Any] = {
            "file": address(item), "members": [{".tag": "email", "email": e} for e in emails],
            "quiet": False, "access_level": {".tag": access_level(role)},
            "add_message_as_comment": False}
        if message:
            arguments["custom_message"] = message
        answer = self.rpc("sharing/add_file_member", arguments, write=True)
        return answer if isinstance(answer, list) else []

    def share_folder(self, path: str) -> Dict[str, Any]:
        return self.rpc("sharing/share_folder", {"path": path, "force_async": False},
                        write=True)

    def add_folder_members(self, shared_folder_id: str, emails: List[str], role: str,
                           message: str) -> None:
        arguments: Dict[str, Any] = {
            "shared_folder_id": shared_folder_id, "quiet": False,
            "members": [{"member": {".tag": "email", "email": e},
                         "access_level": {".tag": access_level(role)}} for e in emails]}
        if message:
            arguments["custom_message"] = message
        self.rpc("sharing/add_folder_member", arguments, write=True)

    def create_link(self, path: str, audience: str, access: str) -> Dict[str, Any]:
        return self.rpc("sharing/create_shared_link_with_settings", {
            "path": path, "settings": {"audience": {".tag": audience},
                                       "access": {".tag": access}}}, write=True)

    def modify_link(self, url: str, audience: str, access: str) -> Dict[str, Any]:
        return self.rpc("sharing/modify_shared_link_settings", {
            "url": url, "settings": {"audience": {".tag": audience},
                                     "access": {".tag": access}}}, write=True)

    def revoke_link(self, url: str) -> None:
        self.rpc("sharing/revoke_shared_link", {"url": url}, write=True)

    def remove_file_member(self, item: str, member: Dict[str, Any]) -> None:
        self.rpc("sharing/remove_file_member_2", {"file": address(item), "member": member},
                 write=True)

    def remove_folder_member(self, shared_folder_id: str, member: Dict[str, Any]) -> None:
        self.rpc("sharing/remove_folder_member", {
            "shared_folder_id": shared_folder_id, "member": member,
            "leave_a_copy": False}, write=True)


def access_level(role: str) -> str:
    return "editor" if role == "write" else "viewer"


def tag(value: Any) -> str:
    return str((value or {}).get(".tag") or "") if isinstance(value, dict) else str(value or "")


def parent_of(path_display: str) -> str:
    if not path_display:
        return ""
    head = path_display.rsplit("/", 1)[0]
    return head or "/"


def item_row(item: Dict[str, Any], people: Optional[DropboxClient] = None) -> Dict[str, Any]:
    """A file or folder as the agent shows it. Dropbox has no web
    address in its metadata, so ``link`` is filled only where Dropbox
    gave one; a made-up URL would be an invented path."""
    kind = tag(item)
    sharing = item.get("sharing_info") or {}
    modified_by = str(sharing.get("modified_by") or "")
    return {
        "item_id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "kind": "folder" if kind == "folder" else "file",
        "size": int(item.get("size") or 0),
        # Dropbox keeps no content type; the name's extension is all there is.
        "mime_type": "" if kind == "folder" else
                     (mimetypes.guess_type(str(item.get("name") or ""))[0] or ""),
        "modified": str(item.get("server_modified") or ""),
        "modified_by": people.person(modified_by) if (people and modified_by) else "",
        "folder": parent_of(str(item.get("path_display") or "")),
        "path": str(item.get("path_display") or ""),
        "link": str(item.get("preview_url") or ""),
        "shared": bool(sharing) or bool(item.get("has_explicit_shared_members")),
    }


ROLES = {"owner": "owner", "editor": "write", "viewer": "read",
         "viewer_no_comment": "read", "traverse": "read"}


def member_rows(members: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Dropbox's users, groups and invitees as permission rows. The id
    says how to take the access away again: ``member:<address>``,
    ``account:<dbid>`` or ``group:<id>``."""
    rows = []
    for user in members.get("users") or []:
        person = user.get("user") or {}
        role = ROLES.get(tag(user.get("access_type")), "read")
        email = str(person.get("email") or "")
        rows.append({
            "permission_id": f"member:{email}" if email else f"account:{person.get('account_id') or ''}",
            "who": email or str(person.get("display_name") or ""),
            "role": "owner" if role == "owner" else role,
            "via": "owner" if role == "owner" else
                   "inherited" if user.get("is_inherited") else "invite",
            "link": ""})
    for group in members.get("groups") or []:
        info = group.get("group") or {}
        role = ROLES.get(tag(group.get("access_type")), "read")
        rows.append({"permission_id": f"group:{info.get('group_id') or ''}",
                     "who": str(info.get("group_name") or ""), "role": role,
                     "via": "inherited" if group.get("is_inherited") else "invite",
                     "link": ""})
    for invitee in members.get("invitees") or []:
        email = str((invitee.get("invitee") or {}).get("email") or "")
        rows.append({"permission_id": f"member:{email}", "who": email,
                     "role": ROLES.get(tag(invitee.get("access_type")), "read"),
                     "via": "inherited" if invitee.get("is_inherited") else "invite",
                     "link": ""})
    return rows


REACH = {"public": "anyone with the link", "team_only": "people in your team",
         "team": "people in your team", "password": "anyone with the link and its password",
         "no_one": "no one (the link is disabled)", "members": "the people the item is shared with"}


def link_row(link: Dict[str, Any]) -> Dict[str, Any]:
    settings = link.get("link_permissions") or {}
    visibility = tag(settings.get("effective_audience")) or \
        tag(settings.get("resolved_visibility")) or "public"
    access = tag(settings.get("link_access_level"))
    url = str(link.get("url") or "")
    return {"permission_id": f"link:{url}", "who": REACH.get(visibility, visibility),
            "role": "write" if access == "editor" else "read", "via": "link", "link": url}
