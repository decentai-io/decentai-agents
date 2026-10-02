"""Where a mail account's servers are, and how this agent reaches them.

An account is an address and a password the person typed once — for
most providers an APP PASSWORD, made in the provider's own settings
for one program to use. The common providers are picked by name and
their servers are known here; any other is reached at the server names
the person gives.

Mail is read over IMAP on port 993 and sent over SMTP on 465, or on
587 where a provider offers only that. Both are encrypted from the
first byte or before the password is said (STARTTLS); this agent never
speaks to a mail server in the clear, except to the loopback servers
of its own tests.

Every connection is opened with the SDK's Tunnel: through the
platform's proxy where agents are confined, where the hosts and ports
below are the ones the manifest declared.
"""

from __future__ import annotations

import ssl
from typing import Any, Dict, List, Optional, Tuple

from decentai_sdk.net import Tunnel, TunnelRefused

IMAP_PORT = 993
SMTP_PORTS = (465, 587)
SECONDS = 30

#: provider -> (imap host, smtp host, the ports its smtp answers on).
PROVIDERS: Dict[str, Tuple[str, str, Tuple[int, ...]]] = {
    "gmail":    ("imap.gmail.com",      "smtp.gmail.com",      (465, 587)),
    "yahoo":    ("imap.mail.yahoo.com", "smtp.mail.yahoo.com", (465, 587)),
    "icloud":   ("imap.mail.me.com",    "smtp.mail.me.com",    (587,)),
    "fastmail": ("imap.fastmail.com",   "smtp.fastmail.com",   (465, 587)),
}

#: Where a person makes an app password, said when a sign-in is refused.
APP_PASSWORDS = {
    "gmail": "Google needs an app password here, not the account's own: turn on "
             "2-Step Verification, then make one at "
             "https://myaccount.google.com/apppasswords",
    "yahoo": "Yahoo needs an app password here: Account security, Generate app password.",
    "icloud": "iCloud needs an app-specific password here: make one at "
              "https://account.apple.com under Sign-In and Security.",
    "fastmail": "Fastmail needs an app password here: Settings, Privacy & Security, "
                "App passwords, with access to mail.",
}


class MailError(Exception):
    """A failure the assistant can word: ``auth`` (the sign-in was
    refused), ``refused`` (the platform would not open the connection),
    ``network`` (the server did not answer), ``server`` (the server
    answered with an error), ``not_found``, ``unknown`` (a message was
    handed over and no answer came back: nobody knows whether it was
    sent)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Account:
    """One connected account: whose it is, its password, and where its
    servers are."""

    def __init__(self, secret: Dict[str, Any]):
        self.email = str(secret.get("account") or "").strip()
        self.password = str(secret.get("password") or "")
        self.provider = str(secret.get("provider") or "other").strip().lower()
        known = PROVIDERS.get(self.provider)
        self.imap_host = self._host(secret.get("imap_host")) or (known[0] if known else "")
        self.smtp_host = self._host(secret.get("smtp_host")) or (known[1] if known else "")
        self.smtp_ports = known[2] if known and not secret.get("smtp_host") else SMTP_PORTS
        #: The tests' loopback servers, spoken to in the clear.
        self.test_imap = self._address(secret.get("test_imap"))
        self.test_smtp = self._address(secret.get("test_smtp"))

    @staticmethod
    def _host(value: Any) -> str:
        """The name in what a person typed: a name, or a whole address."""
        value = str(value or "").strip().lower()
        if "://" in value:
            value = value.split("://", 1)[1]
        return value.split("/", 1)[0].split(":", 1)[0]

    @staticmethod
    def _address(value: Any) -> Optional[Tuple[str, int]]:
        host, colon, port = str(value or "").strip().rpartition(":")
        return (host, int(port)) if colon and port.isdigit() else None

    def problems(self) -> List[str]:
        """Why this account cannot be used, empty when it can."""
        found = []
        if not self.email or "@" not in self.email:
            found.append("The account has no email address.")
        if not self.password:
            found.append("The account has no password.")
        if not self.imap_host and not self.test_imap:
            found.append("The account names no mail server: choose a provider, "
                         "or give the IMAP and SMTP server names.")
        return found

    def hint(self) -> str:
        return APP_PASSWORDS.get(
            self.provider,
            "Most providers need an app password here, made in the account's "
            "security settings, and not the account's own password.")

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def to_imap(self):
        """A connected, encrypted socket to the account's IMAP server."""
        if self.test_imap:
            return self._open(*self.test_imap)
        return self._encrypted(self._open(self.imap_host, IMAP_PORT), self.imap_host)

    def to_smtp(self):
        """(socket, port, encrypted): the first of the server's ports
        that could be opened. On 465 the socket is encrypted already;
        on 587 it is not yet, and STARTTLS follows the greeting."""
        if self.test_smtp:
            return self._open(*self.test_smtp), self.test_smtp[1], None
        last: Optional[MailError] = None
        for port in self.smtp_ports:
            try:
                link = self._open(self.smtp_host, port)
            except MailError as exc:
                last = exc
                continue
            if port == 465:
                return self._encrypted(link, self.smtp_host), port, True
            return link, port, False
        raise last or MailError("network", f"{self.smtp_host} could not be reached.")

    @staticmethod
    def _open(host: str, port: int):
        try:
            return Tunnel.open(host, port, timeout=SECONDS)
        except TunnelRefused as exc:
            if exc.status == 403:
                raise MailError("refused", f"The platform refused the connection "
                                           f"to {host}:{port}: {exc.reason}")
            raise MailError("network", f"{host} could not be reached on port "
                                       f"{port}: {exc.reason}")
        except OSError as exc:
            raise MailError("network", f"{host} could not be reached on port "
                                       f"{port}: {exc}")

    @staticmethod
    def context() -> ssl.SSLContext:
        return ssl.create_default_context()

    @classmethod
    def _encrypted(cls, link, host: str):
        try:
            return cls.context().wrap_socket(link, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            link.close()
            raise MailError("network", f"{host} could not be spoken to safely: {exc}")
