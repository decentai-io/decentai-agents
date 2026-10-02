"""A message as it arrives, read into what the assistant is shown.

A mail server has no conversations, only messages that name the ones
they answer. A conversation is rebuilt from that: a message's thread
is the first message its ``References`` names, else the one it replies
to, else itself. Ids are the messages' own ``Message-ID``s, written
without their angle brackets — the one name a message keeps in every
folder and on every server.
"""

from __future__ import annotations

import email
import email.policy
import html
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Tuple

ID_RE = re.compile(r"<([^<>\s]+)>")
TAG_RE = re.compile(r"<[^>]+>")
DROPPED_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")
BREAK_RE = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6])\s*/?>")


def bare(message_id: Any) -> str:
    """A Message-ID without its brackets."""
    found = ID_RE.search(str(message_id or ""))
    return found.group(1) if found else str(message_id or "").strip().strip("<>")


def bracketed(message_id: str) -> str:
    return f"<{bare(message_id)}>"


def decoded(value: Any) -> str:
    """A header as a person reads it: its encoded words decoded, its
    folds undone."""
    try:
        text = str(make_header(decode_header(str(value or ""))))
    except Exception:
        text = str(value or "")
    return " ".join(text.split())


def parse(raw: bytes) -> Message:
    return email.message_from_bytes(raw or b"", policy=email.policy.compat32)


def header(message: Message, name: str) -> str:
    return decoded(message.get(name))


def ids_in(value: Any) -> List[str]:
    return ID_RE.findall(str(value or ""))


def message_id(message: Message) -> str:
    return bare(message.get("Message-ID"))


def thread_id(message: Message) -> str:
    """The conversation a message belongs to: the first message its
    References names, else the one it answers, else itself."""
    for name in ("References", "In-Reply-To"):
        named = ids_in(message.get(name))
        if named:
            return named[0]
    return message_id(message)


def sent_at(message: Message, fallback: str = "") -> str:
    """When it was sent, as ISO with its offset; the server's own time
    for it where the message does not say."""
    for value in (message.get("Date"), fallback):
        try:
            when = parsedate_to_datetime(str(value or ""))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.isoformat()
    return ""


def received_at(received: str) -> str:
    """When the server received a message, as ISO."""
    return sent_at(Message(), received)


def moment(iso: str) -> datetime:
    try:
        when = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def address(value: Any) -> str:
    """The address alone, lowercase."""
    return parseaddr(decoded(value))[1].lower()


def addresses(*values: Any) -> List[str]:
    return [found.lower() for _, found in getaddresses(
        [decoded(value) for value in values if value]) if found]


def _text_of(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    for charset in (part.get_content_charset(), "utf-8", "latin-1"):
        try:
            return raw.decode(charset or "utf-8")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def plain(markup: str) -> str:
    """The words of an HTML body, where a message has no plain one."""
    text = DROPPED_RE.sub(" ", markup)
    text = BREAK_RE.sub("\n", text)
    text = html.unescape(TAG_RE.sub(" ", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def is_attachment(part: Message) -> bool:
    if part.is_multipart():
        return False
    disposition = str(part.get("Content-Disposition") or "").lower()
    if disposition.startswith("attachment"):
        return True
    return bool(part.get_filename()) and not part.get_content_type().startswith("text/")


def body_and_attachments(message: Message) -> Tuple[str, List[Dict[str, Any]]]:
    """(the body as text, the attachments). An attachment's id is its
    place among the message's attachments, which does not change."""
    texts, markups, attached = [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        if is_attachment(part):
            raw = part.get_payload(decode=True) or b""
            attached.append({
                "attachment_id": f"a{len(attached) + 1}",
                "filename": decoded(part.get_filename()) or f"attachment-{len(attached) + 1}",
                "mime_type": part.get_content_type(),
                "size": len(raw),
            })
        elif part.get_content_type() == "text/plain":
            texts.append(_text_of(part))
        elif part.get_content_type() == "text/html":
            markups.append(_text_of(part))
    text = "\n".join(t.strip() for t in texts if t.strip())
    if not text and markups:
        text = plain("\n".join(markups))
    return text.replace("\r\n", "\n").strip(), attached


def attachment_bytes(message: Message, attachment_id: str) -> bytes:
    wanted, seen = str(attachment_id or ""), 0
    for part in message.walk():
        if is_attachment(part):
            seen += 1
            if wanted == f"a{seen}":
                return part.get_payload(decode=True) or b""
    raise KeyError(attachment_id)


def snippet(text: str, length: int = 160) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= length else words[:length].rstrip() + "…"


def row(message: Message, received: str = "") -> Dict[str, Any]:
    """One message as a search shows it."""
    text, _ = body_and_attachments(message)
    return {
        "message_id": message_id(message),
        "thread_id": thread_id(message),
        "from": header(message, "From"),
        "to": header(message, "To"),
        "date": sent_at(message, received),
        "subject": header(message, "Subject"),
        "snippet": snippet(text),
    }
