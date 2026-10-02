"""A small Microsoft Graph Teams client over the connected account.

One class, no SDK, and the same failure kinds as the other Microsoft
agents' clients:

- ``auth``: the account is not connected, or Microsoft no longer
  accepts its grant — reconnect it from the Credentials page.
- ``http``: Graph answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it, because a retry could say it twice.
- ``not_found``: Teams has no such chat, team, channel or person.

Reads are retried once on a dropped connection; writes never are.

Chat and channel ids look like "19:…@thread.v2", so every one is
escaped before it goes into a path. Teams hands message bodies back as
HTML; they are read as text here, and sent as text.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def quoted(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


class TeamsClient:
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
                 write: bool = False) -> Dict[str, Any]:
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
                    raise GraphError(
                        "unknown", f"No answer from Microsoft for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GraphError("auth", "The Microsoft connection has expired "
                                         "or was revoked — reconnect the "
                                         "account from its Credentials page.")
            if response.status_code == 404:
                raise GraphError("not_found", "Teams has no such chat, team, "
                                              "channel or person.")
            if response.status_code >= 400:
                raise GraphError("http", f"Microsoft refused {method} {path}: "
                                         f"{self._detail(response)}")
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise GraphError("http", f"Microsoft could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "me", params={
            "$select": "id,mail,userPrincipalName,displayName"})

    def chats(self, top: int = 50) -> List[Dict[str, Any]]:
        return self._request("GET", "me/chats", params={
            "$expand": "members", "$top": top}).get("value") or []

    def chat(self, chat_id: str) -> Dict[str, Any]:
        return self._request("GET", f"chats/{quoted(chat_id)}", params={"$expand": "members"})

    def messages(self, chat_id: str, top: int) -> List[Dict[str, Any]]:
        """Newest first, as Graph pages them."""
        return self._request("GET", f"chats/{quoted(chat_id)}/messages", params={
            "$top": top, "$orderby": "createdDateTime desc"}).get("value") or []

    def joined_teams(self) -> List[Dict[str, Any]]:
        return self._request("GET", "me/joinedTeams").get("value") or []

    def channels(self, team_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"teams/{quoted(team_id)}/channels").get("value") or []

    # -- writes ----------------------------------------------------------
    def one_on_one(self, me: str, other: str) -> Dict[str, Any]:
        """The one-to-one chat between two people; Graph answers with the
        existing one when there is one."""
        def member(address: str) -> Dict[str, Any]:
            return {"@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{API_BASE_URL}/users('{address}')"}
        return self._request("POST", "chats", write=True, json={
            "chatType": "oneOnOne", "members": [member(me), member(other)]})

    def send(self, chat_id: str, text: str) -> Dict[str, Any]:
        return self._request("POST", f"chats/{quoted(chat_id)}/messages", write=True,
                             json={"body": {"contentType": "text", "content": text}})

    def post(self, team_id: str, channel_id: str, text: str, subject: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"body": {"contentType": "text", "content": text}}
        if subject:
            body["subject"] = subject
        return self._request("POST", f"teams/{quoted(team_id)}/channels/"
                                     f"{quoted(channel_id)}/messages", json=body, write=True)


def text_of(body: Any) -> str:
    content = str((body or {}).get("content") or "")
    if str((body or {}).get("contentType") or "").lower() == "html":
        content = re.sub(r"<br\s*/?>|</p>|</div>", "\n", content, flags=re.IGNORECASE)
        content = re.sub(r"<[^>]+>", "", content)
        content = html.unescape(content)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def moment(text: Any) -> Optional[datetime]:
    """Graph's timestamps, with any fraction Python cannot read trimmed."""
    raw = str(text or "").replace("Z", "+00:00")
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def members_of(chat: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """Member user ids to (email, name)."""
    return {str(m.get("userId") or ""): (str(m.get("email") or ""), str(m.get("displayName") or ""))
            for m in chat.get("members") or []}


def chat_row(chat: Dict[str, Any], me_id: str) -> Dict[str, Any]:
    others = [m for m in chat.get("members") or [] if str(m.get("userId") or "") != me_id]
    names = [str(m.get("displayName") or m.get("email") or "") for m in others]
    return {"chat_id": str(chat.get("id") or ""), "kind": str(chat.get("chatType") or ""),
            "topic": str(chat.get("topic") or "") or ", ".join(n for n in names if n),
            "members": ", ".join(str(m.get("email") or "") for m in others if m.get("email")),
            "updated": str(chat.get("lastUpdatedDateTime") or ""),
            "link": str(chat.get("webUrl") or "")}


def is_message(message: Dict[str, Any]) -> bool:
    """A person's message — not a system notice, not deleted."""
    return str(message.get("messageType") or "message") == "message" \
        and not message.get("deletedDateTime")


def message_row(message: Dict[str, Any], members: Dict[str, Tuple[str, str]],
                me_id: str) -> Dict[str, Any]:
    user = (message.get("from") or {}).get("user") or {}
    user_id = str(user.get("id") or "")
    email, name = members.get(user_id, ("", ""))
    return {"message_id": str(message.get("id") or ""),
            "from": email or str(user.get("displayName") or ""),
            "from_name": name or str(user.get("displayName") or ""),
            "sent": str(message.get("createdDateTime") or ""),
            "text": text_of(message.get("body")),
            "mine": bool(user_id) and user_id == me_id}
