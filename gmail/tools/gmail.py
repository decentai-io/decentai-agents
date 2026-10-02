"""A small Gmail API client over the bound credential.

One class, no SDK: the Gmail REST surface this agent needs is a dozen
calls, and a hand-written client keeps every failure readable. Three
kinds of failure are told apart, because the assistant must word them
differently:

- ``auth``: the account is not connected, or its grant was revoked —
  reconnect it from the Credentials page.
- ``http``: Gmail answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back (timeout, a
  dropped connection). The outcome is unknown; nothing retries it.

Reads are retried once on a dropped connection; writes never are.
"""

from __future__ import annotations

import base64
import html
import re
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30


class GmailError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def b64url_decode(data: str) -> bytes:
    data = str(data or "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def message_link(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


class GmailClient:
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
            raise GmailError("auth", "The Gmail account is not connected — "
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
            return str(error.get("message") or error)[:300]
        return str(error or body)[:300]

    # -- one request -----------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json=None,
                 write: bool = False) -> Dict[str, Any]:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for attempt in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = requests.request(
                    method, url, params=params, json=json,
                    headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GmailError(
                        "unknown", f"No answer from Gmail for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GmailError("auth", "The Gmail connection has expired or "
                                     "was revoked — reconnect the account "
                                     "from its Credentials page.")
            if response.status_code == 404:
                raise GmailError("not_found", f"Gmail has no {path}.")
            if response.status_code >= 400:
                raise GmailError("http", f"Gmail refused {method} {path}: "
                                         f"{self._detail(response)}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GmailError("http", f"Gmail could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "profile")

    def list_messages(self, query: str, max_results: int,
                      page_token: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "messages", params=params)

    #: How many pages of ids one inbox check will walk. Gmail lists
    #: newest first and cannot be asked for oldest first, so the oldest
    #: of what arrived since the cursor are the LAST ids of the whole
    #: listing; a thousand ids is a month of a busy inbox in one gap.
    MAX_ID_PAGES = 10

    def newest_inbox_message(self) -> Optional[Dict[str, Any]]:
        """The inbox's most recent message, or None when it is empty —
        where an inbox watch starts, in Gmail's own clock."""
        page = self.list_messages("in:inbox", 1)
        ids = [str(m.get("id") or "") for m in page.get("messages") or []]
        return self.get_message(ids[0]) if ids and ids[0] else None

    def inbox_after(self, since_seconds: int, want: int):
        """The ``want`` OLDEST inbox messages received at or after
        ``since_seconds``, oldest first, and how many ids matched in all.

        after: is asked one second early so Gmail's own rounding of it
        never hides the cursor's second; the caller drops what is older
        than the cursor and what it has already handed on."""
        ids: List[str] = []
        token = ""
        for _ in range(self.MAX_ID_PAGES):
            page = self.list_messages(
                f"in:inbox after:{max(since_seconds - 1, 0)}", 100, token)
            ids.extend(str(m.get("id") or "") for m in page.get("messages") or [])
            token = str(page.get("nextPageToken") or "")
            if not token:
                break
        oldest = ids[-want:] if 0 < want < len(ids) else ids
        messages = [self.get_message(i) for i in oldest if i]
        messages.sort(key=lambda m: (internal_seconds(m), str(m.get("id") or "")))
        return messages, len(ids)

    def get_message(self, message_id: str, fmt: str = "metadata") -> Dict[str, Any]:
        params: Dict[str, Any] = {"format": fmt}
        if fmt == "metadata":
            params["metadataHeaders"] = ["From", "To", "Cc", "Date", "Subject",
                                         "Message-ID"]
        return self._request("GET", f"messages/{message_id}", params=params)

    def get_thread(self, thread_id: str) -> Dict[str, Any]:
        return self._request("GET", f"threads/{thread_id}",
                             params={"format": "full"})

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        answer = self._request(
            "GET", f"messages/{message_id}/attachments/{attachment_id}")
        return b64url_decode(str(answer.get("data") or ""))

    # -- writes ----------------------------------------------------------
    def create_draft(self, raw_mime: bytes, thread_id: str = "") -> Dict[str, Any]:
        message: Dict[str, Any] = {"raw": b64url_encode(raw_mime)}
        if thread_id:
            message["threadId"] = thread_id
        return self._request("POST", "drafts", json={"message": message},
                             write=True)

    def send_draft(self, draft_id: str) -> Dict[str, Any]:
        # Gmail sends a draft at drafts/send with the id in the body.
        # There is no drafts/{id}/send: that address answered 404, and
        # every send came back "not found" seconds after the draft was made.
        return self._request("POST", "drafts/send",
                             json={"id": draft_id}, write=True)

    def delete_draft(self, draft_id: str) -> None:
        self._request("DELETE", f"drafts/{draft_id}", write=True)


# ----------------------------------------------------------------------
# Reading a Gmail message document
# ----------------------------------------------------------------------

def header(message: Dict[str, Any], name: str) -> str:
    headers = ((message.get("payload") or {}).get("headers") or [])
    for entry in headers:
        if str(entry.get("name") or "").lower() == name.lower():
            return str(entry.get("value") or "")
    return ""


def internal_seconds(message: Dict[str, Any]) -> int:
    """When Gmail received a message, to the second — its internalDate
    is milliseconds since the epoch, as a string."""
    try:
        return int(str(message.get("internalDate") or "0")) // 1000
    except ValueError:
        return 0


def iso_from_seconds(seconds: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def seconds_from_iso(text: str) -> Optional[int]:
    """A person's ISO 8601 date-time as epoch seconds, or None when the
    text is not a date-time. A naive time is read as UTC."""
    from datetime import datetime, timezone

    text = str(text or "").strip()
    if not text:
        return None
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", text)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def summary_row(message: Dict[str, Any]) -> Dict[str, Any]:
    message_id = str(message.get("id") or "")
    return {
        "message_id": message_id,
        "thread_id": str(message.get("threadId") or ""),
        "from": header(message, "From"),
        "to": header(message, "To"),
        "date": header(message, "Date"),
        "subject": header(message, "Subject"),
        "snippet": html.unescape(str(message.get("snippet") or "")),
        "link": message_link(message_id),
    }


_TAGS = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    return _BLANKS.sub("\n\n", html.unescape(_TAGS.sub("", text))).strip()


def body_and_attachments(message: Dict[str, Any]):
    """The text a person would read, and the attachments listed with the
    ids Gmail needs to fetch them. text/plain wins; html is stripped
    only when there is no plain part."""
    plain: List[str] = []
    rich: List[str] = []
    attachments: List[Dict[str, Any]] = []

    def walk(part: Dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        filename = str(part.get("filename") or "")
        if filename and body.get("attachmentId"):
            attachments.append({
                "attachment_id": str(body["attachmentId"]),
                "filename": filename, "mime_type": mime,
                "size": int(body.get("size") or 0),
            })
        elif body.get("data") and mime.startswith("text/"):
            text = b64url_decode(str(body["data"])).decode("utf-8", "replace")
            (plain if mime == "text/plain" else rich).append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(message.get("payload") or {})
    text = "\n".join(plain).strip() or _strip_html("\n".join(rich))
    return text, attachments
