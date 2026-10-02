"""A small Microsoft Graph mail client over the connected account.

One class, no SDK: the Graph surface this agent needs is a dozen
calls, and a hand-written client keeps every failure readable. The
same three kinds of failure as the Gmail agent, worded the same way:

- ``auth``: the account is not connected, or Microsoft no longer
  accepts its grant — reconnect it from the Credentials page.
- ``http``: Graph answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it.

Reads are retried once on a dropped connection; writes never are.

Two Graph specifics worth knowing. Every request asks for immutable
ids, so a message keeps its id when Outlook moves it from Drafts to
Sent Items — which is what lets a send be confirmed by reading the
same message back. And every request asks for text bodies, so HTML
mail arrives as the text a person would read.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30

MESSAGE_FIELDS = ("id,conversationId,internetMessageId,subject,bodyPreview,"
                  "from,toRecipients,ccRecipients,receivedDateTime,"
                  "sentDateTime,hasAttachments,isDraft,webLink")

#: What an attachment is asked for when its bytes are not wanted yet.
ATTACHMENT_FIELDS = "id,name,contentType,size,isInline"

#: The three shapes Graph returns in an attachments collection. Only a
#: file has bytes: an item attachment is a whole message or event, and a
#: reference attachment is a link to cloud storage. Asking either of
#: those for contentBytes returns nothing, which used to surface as
#: "Outlook returned an empty attachment" — true, and useless.
FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"
ITEM_ATTACHMENT = "#microsoft.graph.itemAttachment"
REFERENCE_ATTACHMENT = "#microsoft.graph.referenceAttachment"


def attachment_kind(item: Dict[str, Any]) -> str:
    """``file``, ``item``, ``reference`` — or ``file`` when Graph did not
    say. The type is polymorphic metadata rather than a selectable field,
    so it is not guaranteed to survive a ``$select``; when it is absent
    the optimistic guess is safe, because a fetch that then comes back
    empty is still refused with its own message."""
    declared = str(item.get("@odata.type") or "").strip()
    if declared == ITEM_ATTACHMENT:
        return "item"
    if declared == REFERENCE_ATTACHMENT:
        return "reference"
    return "file"


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


class GraphClient:
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
                 write: bool = False, absolute: str = "") -> Dict[str, Any]:
        url = absolute or f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for attempt in range(attempts):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"',
            }
            try:
                response = requests.request(
                    method, url, params=params, json=json,
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
                raise GraphError("not_found", f"Outlook has no {path}.")
            if response.status_code >= 400:
                raise GraphError("http", f"Microsoft refused {method} {path}: "
                                         f"{self._detail(response)}")
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise GraphError("http", f"Microsoft could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "me", params={"$select": "mail,userPrincipalName,displayName"})

    def search_messages(self, query: str, max_results: int,
                        page_token: str = "") -> Dict[str, Any]:
        """KQL search over the mailbox. Graph pages a search with a full
        next-link URL, which is what travels as the page token."""
        if page_token:
            return self._request("GET", "", absolute=page_token)
        escaped = query.replace('"', '\\"')
        return self._request("GET", "me/messages", params={
            "$search": f'"{escaped}"', "$top": max_results,
            "$select": MESSAGE_FIELDS,
        })

    def newest_inbox_message(self) -> Optional[Dict[str, Any]]:
        """The inbox's most recent message, or None when it is empty —
        where an inbox watch starts, in Graph's own clock."""
        page = self._request("GET", "me/mailFolders/inbox/messages", params={
            "$orderby": "receivedDateTime desc", "$top": 1,
            "$select": MESSAGE_FIELDS,
        })
        rows = list(page.get("value") or [])
        return rows[0] if rows else None

    def inbox_since(self, since: str, top: int) -> List[Dict[str, Any]]:
        """Inbox messages received at or after ``since`` (ISO 8601, UTC),
        oldest first, at most ``top``. Graph wants the property it
        orders by to lead the filter — receivedDateTime is both."""
        page = self._request("GET", "me/mailFolders/inbox/messages", params={
            "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime asc", "$top": min(top, 100),
            "$select": MESSAGE_FIELDS,
        })
        return list(page.get("value") or [])

    def get_message(self, message_id: str, with_body: bool = False) -> Dict[str, Any]:
        fields = MESSAGE_FIELDS + (",body" if with_body else "")
        return self._request("GET", f"me/messages/{message_id}", params={"$select": fields})

    def get_thread(self, conversation_id: str,
                   limit: int = 0) -> List[Dict[str, Any]]:
        """A conversation's messages, oldest first, bodies included.

        ``limit`` stops the paging. A long conversation with full bodies
        is easily tens of thousands of characters, and a result that big
        cannot be put in front of the assistant whole — it is stored and
        previewed instead, which reads to the person as the assistant
        complaining about their question. Better to return less and say
        so."""
        escaped = conversation_id.replace("'", "''")
        page = self._request("GET", "me/messages", params={
            "$filter": f"conversationId eq '{escaped}'",
            "$orderby": "receivedDateTime asc", "$top": min(limit or 100, 100),
            "$select": MESSAGE_FIELDS + ",body",
        })
        messages = list(page.get("value") or [])
        while page.get("@odata.nextLink"):
            if limit and len(messages) >= limit:
                break
            page = self._request("GET", "", absolute=str(page["@odata.nextLink"]))
            messages.extend(page.get("value") or [])
        return messages[:limit] if limit else messages

    def list_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        """Real attachments of a message, inline images left out.

        ``contentBytes`` is deliberately not selected: the collection
        would otherwise carry every attachment's bytes just to list their
        names."""
        page = self._request("GET", f"me/messages/{message_id}/attachments",
                             params={"$select": ATTACHMENT_FIELDS})
        items = list(page.get("value") or [])
        while page.get("@odata.nextLink"):
            page = self._request("GET", "", absolute=str(page["@odata.nextLink"]))
            items.extend(page.get("value") or [])
        return [a for a in items if not a.get("isInline")]

    def attachment_info(self, message_id: str,
                        attachment_id: str) -> Dict[str, Any]:
        """One attachment's name, type and size WITHOUT its bytes — so a
        caller can decide whether to ask for them at all."""
        return self._request(
            "GET", f"me/messages/{message_id}/attachments/{attachment_id}",
            params={"$select": ATTACHMENT_FIELDS})

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        answer = self._request("GET", f"me/messages/{message_id}/attachments/{attachment_id}")
        return base64.b64decode(str(answer.get("contentBytes") or ""))

    # -- writes ----------------------------------------------------------
    def create_reply(self, message_id: str) -> Dict[str, Any]:
        """A draft reply Outlook addresses itself: the sender, the Re:
        subject, the quoted history, the conversation kept."""
        return self._request("POST", f"me/messages/{message_id}/createReply", json={}, write=True)

    def create_draft(self, to: str, subject: str, body: str, cc: str = "") -> Dict[str, Any]:
        return self._request("POST", "me/messages", json={
            "subject": subject,
            "body": {"contentType": "text", "content": body},
            "toRecipients": recipients(to),
            "ccRecipients": recipients(cc),
        }, write=True)

    def update_draft(self, draft_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", f"me/messages/{draft_id}", json=changes, write=True)

    def send_draft(self, draft_id: str) -> None:
        """Graph answers 202 and nothing else; confirmation is a read of
        the same message afterwards, which the immutable id allows."""
        self._request("POST", f"me/messages/{draft_id}/send", json={}, write=True)

    def delete_draft(self, draft_id: str) -> None:
        self._request("DELETE", f"me/messages/{draft_id}", write=True)


# ----------------------------------------------------------------------
# Reading a Graph message document
# ----------------------------------------------------------------------

def address(entry: Any) -> str:
    """One recipient as 'Name <address>' or the bare address."""
    mailbox = (entry or {}).get("emailAddress") or {}
    name = str(mailbox.get("name") or "").strip()
    addr = str(mailbox.get("address") or "").strip()
    if name and addr and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return addr or name


def addresses(entries: Any) -> str:
    return ", ".join(a for a in (address(e) for e in entries or []) if a)


def bare_address(entry: Any) -> str:
    return str(((entry or {}).get("emailAddress") or {}).get("address") or "").strip().lower()


def recipients(text: str) -> List[Dict[str, Any]]:
    """'a@x, Name <b@y>' → Graph recipient objects."""
    from email.utils import getaddresses

    return [{"emailAddress": {"address": addr, **({"name": name} if name else {})}}
            for name, addr in getaddresses([text or ""]) if addr]


def graph_time(text: str) -> str:
    """A person's ISO 8601 date-time as Graph writes one: UTC, to the
    second, Z. Empty when the text is not a date-time at all. A naive
    time is read as UTC — the cursor is compared with Graph's clock,
    not the person's."""
    import re
    from datetime import datetime, timezone

    text = str(text or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"(\.\d{6})\d+", r"", text)   # Graph writes 7 fraction digits
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summary_row(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "message_id": str(message.get("id") or ""),
        "thread_id": str(message.get("conversationId") or ""),
        "from": address(message.get("from")),
        "to": addresses(message.get("toRecipients")),
        "date": str(message.get("receivedDateTime") or message.get("sentDateTime") or ""),
        "subject": str(message.get("subject") or ""),
        "snippet": str(message.get("bodyPreview") or ""),
        "link": str(message.get("webLink") or ""),
    }


def body_text(message: Dict[str, Any]) -> str:
    """The text body — Graph was asked for text, so HTML only reaches
    here when a server ignored the preference."""
    body = message.get("body") or {}
    content = str(body.get("content") or "")
    if str(body.get("contentType") or "").lower() == "html":
        import html
        import re

        content = re.sub(r"(?is)<(script|style).*?</\1>", "", content)
        content = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", "", content))
        content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()
