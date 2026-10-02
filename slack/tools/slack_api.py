"""A small Slack Web API client over the connected account.

One class, no SDK, and the same failure kinds as the other agents'
clients:

- ``auth``: the account is not connected, or Slack no longer accepts
  its token — reconnect it from the Credentials page.
- ``http``: Slack refused the call — the message names Slack's error.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it, because a retry could post twice.
- ``not_found``: Slack has no such conversation, thread or person.
- ``rate_limited``: Slack asked us to slow down, and said for how long.

Reads are retried once on a dropped connection; writes never are.

Slack answers most failures with HTTP 200 and ``{"ok": false,
"error": "..."}``, so the envelope is read on every answer, not just
the status code. Message ``ts`` values are Slack's ids as well as its
clock; they are passed on exactly as Slack wrote them.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://slack.com/api"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30
TEXT_LIMIT = 1500

AUTH_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed",
               "token_expired"}
NOT_FOUND_ERRORS = {"channel_not_found", "thread_not_found", "user_not_found",
                    "message_not_found"}
# Housekeeping in a conversation, not something anyone said.
NOTICE_SUBTYPES = {"channel_join", "channel_leave", "group_join", "group_leave",
                   "channel_topic", "channel_purpose", "channel_name"}


class SlackError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind   # "auth" | "http" | "unknown" | "not_found" | "rate_limited"
        self.message = message


class SlackClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the sign-in and holds a
        # long-lived user token (token rotation is off). Nothing here
        # refreshes or mints one.
        self.account = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        self.api_base_url = (str(secret.get("api_base_url") or "")
                             or API_BASE_URL).rstrip("/")
        self._me: Optional[Dict[str, Any]] = None

    # -- one call ----------------------------------------------------------
    def _call(self, method: str, params: Optional[Dict[str, Any]] = None, *,
              body: Optional[Dict[str, Any]] = None, write: bool = False) -> Dict[str, Any]:
        if not self.access_token:
            raise SlackError("auth", "The Slack account is not connected — connect "
                                     "it from the agent's Credentials tab.")
        url = f"{self.api_base_url}/{method}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                if write:
                    response = requests.post(url, json=body, headers=headers,
                                             timeout=WRITE_TIMEOUT)
                else:
                    response = requests.get(url, params=params, headers=headers,
                                            timeout=READ_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise SlackError("unknown", f"No answer from Slack for {method}: "
                                                f"the outcome is unknown ({exc}).")
                continue
            return self._read(method, response)
        raise SlackError("http", f"Slack could not be reached: {last}")

    @staticmethod
    def _read(method: str, response: requests.Response) -> Dict[str, Any]:
        if response.status_code == 429:
            wait = response.headers.get("Retry-After") or "a few"
            raise SlackError("rate_limited", f"Slack is rate limiting {method}; try "
                                             f"again after {wait} seconds.")
        if response.status_code >= 400:
            raise SlackError("http", f"Slack refused {method}: HTTP "
                                     f"{response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            raise SlackError("http", f"Slack answered {method} with something "
                                     f"that is not JSON.")
        if data.get("ok"):
            return data
        error = str(data.get("error") or "unknown_error")
        if error in AUTH_ERRORS:
            raise SlackError("auth", f"Slack no longer accepts this connection "
                                     f"({error}) — reconnect the account from its "
                                     f"Credentials page.")
        if error in NOT_FOUND_ERRORS:
            raise SlackError("not_found", f"Slack has no such conversation, thread "
                                          f"or person ({error}).")
        if error == "ratelimited":
            raise SlackError("rate_limited", f"Slack is rate limiting {method}; try "
                                             f"again in a minute.")
        raise SlackError("http", f"Slack refused {method}: {error}.")

    # -- reads -------------------------------------------------------------
    def me(self) -> Dict[str, Any]:
        """auth.test, once per call: who the token is, and the workspace
        URL that permalinks are built from."""
        if self._me is None:
            self._me = self._call("auth.test")
        return self._me

    def my_conversations(self, types: str, most: int = 1000) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        cursor = ""
        while len(rows) < most:
            params: Dict[str, Any] = {"types": types, "exclude_archived": "true",
                                      "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = self._call("users.conversations", params)
            rows.extend(page.get("channels") or [])
            cursor = next_cursor(page)
            if not cursor:
                break
        return rows[:most]

    def conversation(self, channel: str) -> Dict[str, Any]:
        return self._call("conversations.info", {"channel": channel}).get("channel") or {}

    def members(self, channel: str) -> List[str]:
        found = self._call("conversations.members", {"channel": channel, "limit": 50})
        return [str(m) for m in found.get("members") or []]

    def history(self, channel: str, limit: int, oldest: str = "", latest: str = "",
                cursor: str = "") -> Dict[str, Any]:
        """Newest first, as Slack pages them."""
        params: Dict[str, Any] = {"channel": channel, "limit": limit}
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest
        if cursor:
            params["cursor"] = cursor
        return self._call("conversations.history", params)

    def history_since(self, channel: str, oldest: str, pages: int = 5) -> List[Dict[str, Any]]:
        """Every message after ``oldest`` (Slack's oldest is exclusive),
        oldest first. Pages run newest to oldest, so all of them are read
        before the oldest is known — bounded, because a check runs often."""
        found: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(pages):
            page = self.history(channel, 100, oldest=oldest, cursor=cursor)
            found.extend(page.get("messages") or [])
            cursor = next_cursor(page)
            if not (page.get("has_more") and cursor):
                break
        return sorted(found, key=lambda m: ts_number(m.get("ts")))

    def replies(self, channel: str, ts: str, limit: int) -> Dict[str, Any]:
        """The thread's parent first, then its replies oldest first."""
        return self._call("conversations.replies", {"channel": channel, "ts": ts,
                                                    "limit": limit})

    def search(self, query: str, count: int, page: int = 1) -> Dict[str, Any]:
        return self._call("search.messages", {
            "query": query, "count": count, "page": page,
            "sort": "timestamp", "sort_dir": "desc"}).get("messages") or {}

    def user(self, user_id: str) -> Dict[str, Any]:
        return self._call("users.info", {"user": user_id}).get("user") or {}

    # -- writes ------------------------------------------------------------
    def post(self, channel: str, text: str, thread_ts: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        return self._call("chat.postMessage", body=body, write=True)


class People:
    """User ids to display names, each asked of Slack at most once per
    call — twenty messages usually have three authors."""

    def __init__(self, client: SlackClient):
        self.client = client
        self._names: Dict[str, str] = {}

    def name(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id not in self._names:
            try:
                user = self.client.user(user_id)
            except SlackError as exc:
                # A deleted or foreign user keeps their id rather than
                # failing the whole answer.
                if exc.kind != "not_found":
                    raise
                user = {}
            profile = user.get("profile") or {}
            self._names[user_id] = str(profile.get("display_name") or profile.get("real_name")
                                       or user.get("real_name") or user.get("name") or user_id)
        return self._names[user_id]

    def author(self, message: Dict[str, Any]) -> str:
        if message.get("user"):
            return self.name(str(message["user"]))
        bot = message.get("bot_profile") or {}
        return str(message.get("username") or bot.get("name") or message.get("bot_id") or "")

    def readable(self, text: Any) -> str:
        """Slack's markup as text: <@U…> as @name, <#C…|name> as #name,
        <url|label> as label (url), and its three escapes undone. Clipped,
        so one long paste cannot crowd out a whole answer."""
        def mention(match):
            return "@" + self.name(match.group(1))

        out = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", mention, str(text or ""))
        out = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", out)
        out = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", out)
        out = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", out)
        out = re.sub(r"<(https?://[^>]+)>", r"\1", out)
        return clip(html.unescape(out))


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def next_cursor(page: Dict[str, Any]) -> str:
    return str((page.get("response_metadata") or {}).get("next_cursor") or "")


def ts_number(ts: Any) -> Decimal:
    """Slack's "1712345678.123456" as an exact number: a float would
    round away the microseconds that tell two messages apart."""
    try:
        return Decimal(str(ts))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def is_ts(text: str) -> bool:
    return bool(re.fullmatch(r"\d{9,11}(\.\d{1,6})?", text or ""))


def ts_time(ts: Any) -> str:
    """When a ts was, in UTC, for people to read; the ts stays the id."""
    try:
        moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def permalink(team_url: str, channel: str, ts: str, thread_ts: str = "") -> str:
    """Slack's archive link, built rather than fetched: chat.getPermalink
    would be one more call for every message shown."""
    if not (team_url and channel and ts):
        return ""
    link = f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        link += f"?thread_ts={thread_ts}&cid={channel}"
    return link


def kind_of(conversation: Dict[str, Any]) -> str:
    if conversation.get("is_im"):
        return "dm"
    if conversation.get("is_mpim"):
        return "group_dm"
    if conversation.get("is_private") or conversation.get("is_group"):
        return "private_channel"
    return "channel"


def is_said(message: Dict[str, Any]) -> bool:
    """Something a person (or bot) said, not a join or topic notice."""
    return str(message.get("subtype") or "") not in NOTICE_SUBTYPES


def message_row(message: Dict[str, Any], channel: str, people: People,
                me_id: str, team_url: str) -> Dict[str, Any]:
    ts = str(message.get("ts") or "")
    thread_ts = str(message.get("thread_ts") or "")
    user_id = str(message.get("user") or "")
    return {"ts": ts, "thread_ts": thread_ts, "sent": ts_time(ts),
            "author": people.author(message), "author_id": user_id,
            "mine": bool(me_id) and user_id == me_id,
            "text": people.readable(message.get("text")),
            "reply_count": int(message.get("reply_count") or 0),
            "link": permalink(team_url, channel, ts, thread_ts)}


def conversation_name(conversation: Dict[str, Any], people: People, client: SlackClient,
                      me_id: str) -> str:
    """A channel by its name; a DM by the other person; a group DM by
    everyone else in it."""
    kind = kind_of(conversation)
    if kind == "dm":
        return people.name(str(conversation.get("user") or ""))
    if kind == "group_dm":
        others = [u for u in client.members(str(conversation.get("id") or "")) if u != me_id]
        return ", ".join(people.name(u) for u in others)
    return str(conversation.get("name") or conversation.get("id") or "")
