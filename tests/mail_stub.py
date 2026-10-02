"""A loopback mail server for the Mail agent: one account's folders over
IMAP and its outgoing mail over SMTP, speaking as much of each protocol
as the agent uses, in the clear.

    stub = MailStub().start()
    stub.add("INBOX", "dana@harbourline.example", "New office", "We are moving…")
    stub.secret()         the credential a person would have typed
    stub.sent             what was handed to the SMTP server

What a test can make it do: refuse the sign-in, refuse a recipient,
take a message and never answer, keep what it sends in the Sent folder
as Gmail does, or renumber the inbox.
"""

from __future__ import annotations

import base64
import email
import email.policy
import re
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Dict, List, Optional

DUBAI = timezone(timedelta(hours=4))


class Stored:
    def __init__(self, uid: int, raw: bytes, flags, received: datetime):
        self.uid = uid
        self.raw = raw
        self.flags = set(flags)
        self.received = received

    @property
    def message(self):
        return email.message_from_bytes(self.raw, policy=email.policy.compat32)

    @property
    def head(self) -> bytes:
        return self.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"

    @property
    def text(self) -> bytes:
        parts = self.raw.split(b"\r\n\r\n", 1)
        return parts[1] if len(parts) > 1 else b""

    def words(self) -> str:
        found = [self.head.decode("utf-8", "replace")]
        for part in self.message.walk():
            if part.is_multipart():
                continue
            raw = part.get_payload(decode=True) or b""
            found.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
        return "\n".join(found).lower()


class Folder:
    def __init__(self, name: str, flags=()):
        self.name = name
        self.flags = list(flags)
        self.validity = 1700000000
        self.next = 1
        self.messages: List[Stored] = []

    def put(self, raw: bytes, flags=(), received: Optional[datetime] = None) -> Stored:
        stored = Stored(self.next, raw, flags,
                        received or datetime.now(timezone.utc))
        self.next += 1
        self.messages.append(stored)
        return stored


class MailStub:
    ACCOUNT = "sara@sidra.example"
    PASSWORD = "app-pass-1"

    def __init__(self, archive: bool = False):
        self.folders: Dict[str, Folder] = {
            "INBOX": Folder("INBOX"),
            "Sent Items": Folder("Sent Items", ["\\Sent"]),
        }
        if archive:
            self.folders["[Mail]/All Mail"] = Folder("[Mail]/All Mail", ["\\All"])
        #: What was handed to the SMTP server: (from, [to], raw).
        self.sent: List[tuple] = []
        self.refused_recipients: set = set()
        self.never_answers = False
        self.keeps_sent = False
        self.logins = 0
        self.commands: List[str] = []
        self._servers = []

    # -- what it holds -----------------------------------------------------
    def add(self, folder: str, sender: str, subject: str, body: str,
            date: str = "", to: str = "", message_id: str = "",
            in_reply_to: str = "", references: str = "", attachments=(),
            html: str = "", seen: bool = False, cc: str = "") -> str:
        """Put a message in a folder. Returns its Message-ID, without
        brackets. ``date`` is ISO with an offset."""
        when = (datetime.fromisoformat(date) if date
                else datetime.now(timezone.utc))
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to or self.ACCOUNT
        if cc:
            message["Cc"] = cc
        message["Subject"] = subject
        message["Date"] = format_datetime(when)
        message["Message-ID"] = f"<{message_id}>" if message_id else make_msgid(
            domain=sender.rsplit("@", 1)[-1].strip(">"))
        if in_reply_to:
            message["In-Reply-To"] = f"<{in_reply_to}>"
            message["References"] = " ".join(
                f"<{found}>" for found in (references or in_reply_to).split())
        if html and not body:
            message.set_content(html, subtype="html")
        else:
            message.set_content(body)
            if html:
                message.add_alternative(html, subtype="html")
        for filename, mime_type, content in attachments:
            maintype, subtype = mime_type.split("/", 1)
            message.add_attachment(content, maintype=maintype, subtype=subtype,
                                   filename=filename)
        raw = message.as_bytes(policy=email.policy.SMTP)
        self.folders[folder].put(raw, ["\\Seen"] if seen else [], when)
        archive = self.folders.get("[Mail]/All Mail")
        if archive is not None and folder != archive.name:
            archive.put(raw, ["\\Seen"] if seen else [], when)
        return str(message["Message-ID"]).strip("<>")

    def renumber(self, folder: str = "INBOX") -> None:
        found = self.folders[folder]
        found.validity += 1
        for position, stored in enumerate(found.messages, 1):
            stored.uid = position
        found.next = len(found.messages) + 1

    # -- the credential ------------------------------------------------------
    def secret(self, password: str = "") -> dict:
        return {"account": self.ACCOUNT, "password": password or self.PASSWORD,
                "provider": "other",
                "test_imap": f"127.0.0.1:{self.imap_port}",
                "test_smtp": f"127.0.0.1:{self.smtp_port}"}

    # -- the servers ----------------------------------------------------------
    def start(self) -> "MailStub":
        stub = self

        class Imap(ImapSession):
            mail = stub

        class Smtp(SmtpSession):
            mail = stub

        for handler in (Imap, Smtp):
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self._servers.append(server)
        self.imap_port = self._servers[0].server_address[1]
        self.smtp_port = self._servers[1].server_address[1]
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()


# ----------------------------------------------------------------------
# IMAP
# ----------------------------------------------------------------------

TOKEN_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"|(\([^)]*\))|([^\s]+)')
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


class ImapSession(socketserver.StreamRequestHandler):
    mail: MailStub

    def say(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        self.folder: Optional[Folder] = None
        self.signed_in = False
        self.say("* OK the mail stub is ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            literals = []
            # A literal: the count is announced, the server says go on,
            # the bytes follow, and then the rest of the line.
            while re.search(rb"\{(\d+)\}\r\n$", line):
                count = int(re.search(rb"\{(\d+)\}\r\n$", line).group(1))
                self.say("+ go on")
                literals.append(self.rfile.read(count))
                line = (re.sub(rb"\{\d+\}\r\n$", b"\x00", line)
                        + self.rfile.readline())
            try:
                tag, _, rest = line.strip().partition(b" ")
                if not self.command(tag.decode(), rest, literals):
                    return
            except (BrokenPipeError, ConnectionError):
                return

    def tokens(self, rest: bytes, literals) -> List[str]:
        found, waiting = [], list(literals)
        for quoted, listed, atom in TOKEN_RE.findall(rest):
            if atom == b"\x00":
                found.append(waiting.pop(0).decode("utf-8", "replace"))
            elif listed:
                found.append(listed.decode("utf-8"))
            elif atom:
                found.append(atom.decode("utf-8"))
            else:
                found.append(quoted.decode("utf-8").replace('\\"', '"')
                             .replace("\\\\", "\\"))
        return found

    def command(self, tag: str, rest: bytes, literals) -> bool:
        words = self.tokens(rest, literals)
        name = words[0].upper() if words else ""
        if name == "UID" and len(words) > 1:
            name = f"UID {words[1].upper()}"
            words = words[1:]
        self.mail.commands.append(name)
        arguments = words[1:]

        if name == "CAPABILITY":
            self.say("* CAPABILITY IMAP4rev1 SPECIAL-USE")
        elif name == "LOGIN":
            self.mail.logins += 1
            if arguments[:2] != [self.mail.ACCOUNT, self.mail.PASSWORD]:
                self.say(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
                return True
            self.signed_in = True
        elif name == "LOGOUT":
            self.say("* BYE see you")
            self.say(f"{tag} OK done")
            return False
        elif not self.signed_in:
            self.say(f"{tag} NO sign in first")
            return True
        elif name == "LIST":
            for folder in self.mail.folders.values():
                flags = " ".join(["\\HasNoChildren", *folder.flags])
                self.say(f'* LIST ({flags}) "/" "{folder.name}"')
        elif name in ("SELECT", "EXAMINE"):
            found = self.mail.folders.get(arguments[0])
            if found is None:
                self.say(f"{tag} NO no such folder")
                return True
            self.folder = found
            self.say(f"* {len(found.messages)} EXISTS")
            self.say(f"* OK [UIDVALIDITY {found.validity}] the folder's validity")
            self.say(f"* OK [UIDNEXT {found.next}] the next number")
            self.say(f"{tag} OK [READ-ONLY] opened")
            return True
        elif name == "UID SEARCH":
            found = [str(stored.uid) for stored in self.folder.messages
                     if Search(arguments, self.folder).matches(stored)]
            self.say("* SEARCH " + " ".join(found) if found else "* SEARCH")
        elif name == "UID FETCH":
            self.fetch(arguments)
        elif name == "APPEND":
            folder = self.mail.folders.get(arguments[0])
            if folder is None:
                self.say(f"{tag} NO [TRYCREATE] no such folder")
                return True
            flags = [flag for flag in arguments[1].strip("()").split()] \
                if len(arguments) > 2 else []
            stored = folder.put(arguments[-1].encode("utf-8"), flags)
            self.say(f"{tag} OK [APPENDUID {folder.validity} {stored.uid}] kept")
            return True
        elif name in ("NOOP", "CLOSE"):
            pass
        else:
            self.say(f"{tag} BAD the stub does not speak {name}")
            return True
        self.say(f"{tag} OK done")
        return True

    def fetch(self, arguments) -> None:
        wanted = int(arguments[0])
        items = arguments[1].upper()
        for position, stored in enumerate(self.folder.messages, 1):
            if stored.uid != wanted:
                continue
            said = [f"UID {stored.uid}"]
            pieces = []
            if "FLAGS" in items:
                said.append("FLAGS (" + " ".join(sorted(stored.flags)) + ")")
            if "INTERNALDATE" in items:
                said.append('INTERNALDATE "'
                            + stored.received.strftime("%d-%b-%Y %H:%M:%S %z") + '"')
            if "BODY.PEEK[HEADER]" in items:
                pieces.append(("BODY[HEADER]", stored.head))
            partial = re.search(r"BODY\.PEEK\[TEXT\]<0\.(\d+)>", items)
            if partial:
                pieces.append(("BODY[TEXT]<0>", stored.text[:int(partial.group(1))]))
            if "BODY.PEEK[]" in items:
                pieces.append(("BODY[]", stored.raw))
            self.wfile.write(f"* {position} FETCH ({' '.join(said)}".encode())
            for label, content in pieces:
                self.wfile.write(f" {label} {{{len(content)}}}\r\n".encode() + content)
            self.wfile.write(b")\r\n")
            self.wfile.flush()


class Search:
    """The search keys the agent sends, read against one message."""

    def __init__(self, words: List[str], folder: Folder):
        self.words = list(words)
        if self.words[:1] and self.words[0].upper() == "CHARSET":
            self.words = self.words[2:]
        self.folder = folder

    def matches(self, stored: Stored) -> bool:
        words = list(self.words)
        while words:
            if not self.key(words, stored):
                return False
        return True

    def key(self, words: List[str], stored: Stored) -> bool:
        name = words.pop(0).upper()
        message = stored.message
        if name == "ALL":
            return True
        if name == "UNSEEN":
            return "\\Seen" not in stored.flags
        if name == "SEEN":
            return "\\Seen" in stored.flags
        if name in ("FROM", "TO", "CC", "SUBJECT"):
            return words.pop(0).lower() in self.header(message, name).lower()
        if name in ("TEXT", "BODY"):
            return words.pop(0).lower() in stored.words()
        if name == "HEADER":
            header, value = words.pop(0), words.pop(0)
            return value.lower() in str(message.get(header) or "").lower()
        if name in ("SINCE", "BEFORE", "ON"):
            day, month, year = words.pop(0).split("-")
            asked = (int(year), MONTHS[month.lower()], int(day))
            received = stored.received
            have = (received.year, received.month, received.day)
            return {"SINCE": have >= asked, "BEFORE": have < asked,
                    "ON": have == asked}[name]
        if name == "OR":
            first = self.key(words, stored)
            second = self.key(words, stored)
            return first or second
        if name == "NOT":
            return not self.key(words, stored)
        if name == "UID":
            return self.among(words.pop(0), stored.uid)
        raise ValueError(f"the stub does not search by {name}")

    @staticmethod
    def header(message, name: str) -> str:
        from email.header import decode_header, make_header

        return str(make_header(decode_header(str(message.get(name) or ""))))

    def among(self, numbers: str, uid: int) -> bool:
        largest = max((stored.uid for stored in self.folder.messages), default=0)
        for part in numbers.split(","):
            first, colon, last = part.partition(":")
            low = largest if first == "*" else int(first)
            high = low if not colon else (largest if last == "*" else int(last))
            # As a server does: n:* names the last message even when
            # that is older than n.
            if min(low, high) <= uid <= max(low, high):
                return True
        return False


# ----------------------------------------------------------------------
# SMTP
# ----------------------------------------------------------------------

class SmtpSession(socketserver.StreamRequestHandler):
    mail: MailStub

    def say(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        self.say("220 the mail stub is ready")
        sender, recipients, signed_in = "", [], False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            word, _, rest = line.decode("utf-8", "replace").strip().partition(" ")
            word = word.upper()
            if word in ("EHLO", "HELO"):
                self.say("250-the mail stub")
                self.say("250-AUTH PLAIN LOGIN")
                self.say("250 OK")
            elif word == "AUTH":
                kind, _, given = rest.partition(" ")
                if kind.upper() == "PLAIN":
                    if not given:
                        self.say("334 ")
                        given = self.rfile.readline().decode().strip()
                    _, user, password = base64.b64decode(given).decode().split("\0")
                else:
                    self.say("334 " + base64.b64encode(b"Username:").decode())
                    user = base64.b64decode(self.rfile.readline().strip()).decode()
                    self.say("334 " + base64.b64encode(b"Password:").decode())
                    password = base64.b64decode(self.rfile.readline().strip()).decode()
                self.mail.logins += 1
                signed_in = (user, password) == (self.mail.ACCOUNT, self.mail.PASSWORD)
                self.say("235 signed in" if signed_in
                         else "535 5.7.8 Username and Password not accepted")
            elif word == "MAIL":
                if not signed_in:
                    self.say("530 sign in first")
                    continue
                sender, recipients = rest, []
                self.say("250 OK")
            elif word == "RCPT":
                found = re.search(r"<([^>]+)>", rest)
                address = found.group(1).lower() if found else rest
                if address in self.mail.refused_recipients:
                    self.say("550 5.1.1 no such person here")
                else:
                    recipients.append(address)
                    self.say("250 OK")
            elif word == "DATA":
                self.say("354 go on")
                raw = b""
                while not raw.endswith(b"\r\n.\r\n"):
                    chunk = self.rfile.readline()
                    if not chunk:
                        return
                    raw += chunk
                raw = raw[:-3].replace(b"\r\n..", b"\r\n.")
                if self.mail.never_answers:
                    self.mail.sent.append((sender, list(recipients), raw))
                    return                  # hangs up without a word
                self.mail.sent.append((sender, list(recipients), raw))
                if self.mail.keeps_sent:
                    self.mail.folders["Sent Items"].put(raw, ["\\Seen"])
                self.say("250 OK taken")
            elif word == "RSET":
                sender, recipients = "", []
                self.say("250 OK")
            elif word == "NOOP":
                self.say("250 OK")
            elif word == "QUIT":
                self.say("221 see you")
                return
            else:
                self.say(f"502 the stub does not speak {word}")
