"""One account's mailbox, over IMAP.

Opened for a call and closed with it: a worker holds no state, and a
mail server counts its open connections. Nothing here changes a
message — folders are opened to be read, and the one thing written is
a copy of what this agent sent, appended to the account's Sent folder
where the server did not keep one itself.

A message is found by its own Message-ID in the folders a conversation
lives in: the account's archive of everything where it has one (Gmail's
All Mail), else its inbox and its Sent folder.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from datetime import datetime
from email.message import Message
from email.utils import format_datetime
from typing import Dict, List, Optional, Tuple

from . import messages
from .servers import Account, MailError

LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")

#: How much of a body a search reads to show its first words.
SNIPPET_BYTES = 4096

#: What a Sent folder is called where the server does not say which
#: one it is.
SENT_NAMES = ("sent", "sent items", "sent mail", "sent messages", "inbox.sent")


class _Connection(imaplib.IMAP4):
    """imaplib, over a socket that is already open and encrypted."""

    def __init__(self, link):
        self._link = link
        super().__init__("", 0)

    def _create_socket(self, timeout=None):
        return self._link


def quoted(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    INBOX = "INBOX"

    def __init__(self, account: Account):
        self.account = account
        self.connection: Optional[_Connection] = None
        self.selected = ""
        self._folders: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------
    # Its life
    # ------------------------------------------------------------------

    def __enter__(self) -> "Mailbox":
        problems = self.account.problems()
        if problems:
            raise MailError("auth", " ".join(problems))
        link = self.account.to_imap()
        try:
            self.connection = _Connection(link)
        except (imaplib.IMAP4.error, OSError) as exc:
            link.close()
            raise MailError("network", f"{self.account.imap_host or 'The mail server'} "
                                       f"did not greet: {exc}")
        try:
            self.connection.login(self.account.email, self.account.password)
        except imaplib.IMAP4.error as exc:
            self.close()
            raise MailError(
                "auth", f"The mail server refused the sign-in for "
                        f"{self.account.email} ({self._said(exc)}). "
                        f"{self.account.hint()}")
        except (OSError, ssl.SSLError) as exc:
            self.close()
            raise MailError("network", f"The mail server stopped answering: {exc}")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            connection.logout()
        except Exception:
            try:
                connection.shutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # One command
    # ------------------------------------------------------------------

    def ask(self, what: str, command, *arguments):
        """Run one command and return its data; a refusal and a dropped
        connection become a MailError that says which."""
        try:
            status, data = command(*arguments)
        except imaplib.IMAP4.abort as exc:
            raise MailError("network", f"The mail server hung up while {what}: "
                                       f"{self._said(exc)}")
        except imaplib.IMAP4.error as exc:
            raise MailError("server", f"The mail server refused {what}: "
                                      f"{self._said(exc)}")
        except (OSError, ssl.SSLError, socket.timeout) as exc:
            raise MailError("network", f"The mail server did not answer while "
                                       f"{what}: {exc}")
        if status != "OK":
            raise MailError("server", f"The mail server refused {what}: "
                                      f"{self._said(data)}")
        return data

    @staticmethod
    def _said(found) -> str:
        if isinstance(found, (list, tuple)):
            found = b" ".join(item for item in found if isinstance(item, bytes))
        if isinstance(found, bytes):
            found = found.decode("utf-8", "replace")
        return " ".join(str(found).replace("b'", "").replace("'", "").split())[:200]

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def folders(self) -> Dict[str, str]:
        """The folders that matter, by what they are: ``all`` (the
        archive of everything, where the account has one) and ``sent``.
        A server says which is which; where it does not, a Sent folder
        is known by its name."""
        if self._folders is not None:
            return self._folders
        found: Dict[str, str] = {}
        names = []
        for line in self.ask("listing the folders", self.connection.list):
            if not isinstance(line, bytes):
                continue
            match = LIST_RE.match(line)
            if match is None:
                continue
            flags = match.group("flags").decode("ascii", "replace").lower().split()
            name = match.group("name").decode("utf-8", "replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if "\\noselect" in flags:
                continue
            names.append(name)
            for flag, kind in (("\\all", "all"), ("\\sent", "sent"),
                               ("\\drafts", "drafts")):
                if flag in flags:
                    found.setdefault(kind, name)
        if "sent" not in found:
            for name in names:
                if name.lower().rsplit("/", 1)[-1].rsplit(".", 1)[-1] in SENT_NAMES \
                        or name.lower() in SENT_NAMES:
                    found["sent"] = name
                    break
        self._folders = found
        return found

    def scope(self) -> List[str]:
        """The folders a conversation lives in."""
        folders = self.folders()
        if folders.get("all"):
            return [folders["all"]]
        return [self.INBOX] + ([folders["sent"]] if folders.get("sent") else [])

    def open(self, folder: str) -> Tuple[int, int]:
        """Open a folder to read it. Returns what the server calls the
        folder's validity and the next id it will give: the two numbers
        an inbox watch remembers."""
        self.ask(f"opening {folder}", self.connection.select, quoted(folder), True)
        self.selected = folder
        return (self._number("UIDVALIDITY"), self._number("UIDNEXT"))

    def _number(self, name: str) -> int:
        try:
            _, found = self.connection.response(name)
            return int(found[0]) if found and found[0] is not None else 0
        except (ValueError, TypeError, imaplib.IMAP4.error):
            return 0

    # ------------------------------------------------------------------
    # Finding
    # ------------------------------------------------------------------

    def search(self, criteria: List[str], text: str = "") -> List[int]:
        """The ids, in the open folder, of the messages that match:
        oldest first. ``text`` is words to find anywhere in a message,
        sent as they are whatever alphabet they are in."""
        arguments = list(criteria) or ["ALL"]
        if text:
            self.connection.literal = text.encode("utf-8")
            arguments = ["CHARSET", "UTF-8", *([] if arguments == ["ALL"] else arguments),
                         "TEXT"]
        data = self.ask("searching", self.connection.uid, "SEARCH", *arguments)
        found = b" ".join(item for item in data if isinstance(item, bytes))
        return sorted(int(uid) for uid in found.split() if uid.isdigit())

    def after(self, last_uid: int) -> List[int]:
        """The ids that came after one. A server answers ``n:*`` with
        its last message even when that is older than n, so the answer
        is read, not believed."""
        return [uid for uid in self.search(["UID", f"{int(last_uid) + 1}:*"])
                if uid > int(last_uid)]

    def by_id(self, message_id: str) -> Optional[Tuple[str, int]]:
        """(folder, id) of the message with this Message-ID."""
        for folder in self.scope():
            self.open(folder)
            found = self.search(
                ["HEADER", "Message-ID", quoted(messages.bracketed(message_id))])
            if found:
                return folder, found[-1]
        return None

    def conversation(self, thread_id: str) -> List[Tuple[Message, str]]:
        """Every message of a conversation, oldest first, each with
        the time the server received it. A message that is in two
        folders is there once."""
        named = quoted(messages.bracketed(thread_id))
        found: Dict[str, Tuple[Message, str]] = {}
        for folder in self.scope():
            self.open(folder)
            for uid in self.search(["OR", "HEADER", "Message-ID", named,
                                    "HEADER", "References", named]):
                message, received = self.whole(uid)
                found.setdefault(messages.message_id(message) or f"{folder}:{uid}",
                                 (message, received))
        return sorted(found.values(),
                      key=lambda pair: messages.moment(messages.sent_at(*pair)))

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def glance(self, uid: int) -> Tuple[Message, str, bool]:
        """A message's headers and the start of its body: (message,
        when the server received it, whether it was read)."""
        data = self.ask(
            "reading a message", self.connection.uid, "FETCH", str(int(uid)),
            f"(FLAGS INTERNALDATE BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{SNIPPET_BYTES}>)")
        head, text, about = b"", b"", b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                about += item[0]
                if b"BODY[HEADER]" in item[0]:
                    head = item[1] or b""
                elif b"BODY[TEXT]" in item[0]:
                    text = item[1] or b""
            elif isinstance(item, bytes):
                about += item
        if not head:
            raise MailError("not_found", "The mail server no longer has that message.")
        flags = FLAGS_RE.search(about)
        seen = b"\\seen" in (flags.group(1).lower() if flags else b"")
        return messages.parse(head + text), self._received(about), seen

    def whole(self, uid: int) -> Tuple[Message, str]:
        """(the message, when the server received it)."""
        data = self.ask("reading a message", self.connection.uid, "FETCH",
                        str(int(uid)), "(INTERNALDATE BODY.PEEK[])")
        raw, about = b"", b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                about += item[0]
                raw = item[1] or raw
            elif isinstance(item, bytes):
                about += item
        if not raw:
            raise MailError("not_found", "The mail server no longer has that message.")
        return messages.parse(raw), self._received(about)

    @staticmethod
    def _received(about: bytes) -> str:
        found = INTERNALDATE_RE.search(about)
        if found is None:
            return ""
        try:
            when = datetime.strptime(found.group(1).decode("ascii").strip(),
                                     "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            return ""
        return format_datetime(when)

    # ------------------------------------------------------------------
    # The one thing written
    # ------------------------------------------------------------------

    def keep_sent(self, raw: bytes, message_id: str) -> bool:
        """Put a copy of a sent message in the Sent folder unless the
        server kept one itself. Returns whether a copy is there."""
        sent = self.folders().get("sent")
        if not sent:
            return False
        self.open(sent)
        if self.search(["HEADER", "Message-ID",
                        quoted(messages.bracketed(message_id))]):
            return True
        self.ask("keeping a copy of what was sent", self.connection.append,
                 quoted(sent), "(\\Seen)", None, raw)
        return True
