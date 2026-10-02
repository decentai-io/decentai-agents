"""Sending, over SMTP.

A message is handed to the account's own mail server, which sends it
as the account. Three outcomes are told apart, because the assistant
must word them differently: the server took the message (``sent``), the
server refused it and nothing left (a MailError that says why), or the
message was handed over and no answer came back (``unknown`` — nobody
knows whether it was sent, and nothing here tries again).
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Tuple

from . import messages
from .servers import Account, MailError, SECONDS


class _Connection(smtplib.SMTP):
    """smtplib, over a socket that is already open. ``host`` is the
    server's name: what its certificate is checked against when the
    connection is encrypted after the greeting (STARTTLS)."""

    def __init__(self, link, host: str):
        self._link = link
        super().__init__(timeout=SECONDS)
        self._host = host

    def _get_socket(self, host, port, timeout):
        return self._link


class Outgoing:
    def __init__(self, account: Account):
        self.account = account

    def written(self, to: str, subject: str, body: str, cc: str = "",
                in_reply_to: str = "", references: str = "") -> Tuple[bytes, str]:
        """(the message as it travels, its Message-ID without
        brackets)."""
        message = EmailMessage()
        message["From"] = self.account.email
        message["To"] = to
        if cc:
            message["Cc"] = cc
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        domain = self.account.email.rsplit("@", 1)[-1] or "localhost"
        message["Message-ID"] = make_msgid(domain=domain)
        if in_reply_to:
            message["In-Reply-To"] = messages.bracketed(in_reply_to)
            chain = [messages.bracketed(found) for found in
                     messages.ids_in(references)]
            if messages.bracketed(in_reply_to) not in chain:
                chain.append(messages.bracketed(in_reply_to))
            message["References"] = " ".join(chain)
        message.set_content(body)
        return message.as_bytes(), messages.bare(message["Message-ID"])

    def send(self, raw: bytes, recipients: List[str]) -> None:
        """Hand the message to the server. Returns when the server took
        it; raises MailError otherwise, ``unknown`` when nobody knows."""
        if not recipients:
            raise MailError("server", "The message names nobody to send it to.")
        link, port, encrypted = self.account.to_smtp()
        host = self.account.smtp_host or "localhost"
        connection = _Connection(link, host)
        try:
            try:
                connection.connect(host, port)
                connection.ehlo()
                if encrypted is False:
                    connection.starttls(context=Account.context())
                    connection.ehlo()
                connection.login(self.account.email, self.account.password)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailError(
                    "auth", f"The mail server refused the sign-in for "
                            f"{self.account.email} ({self._said(exc)}). "
                            f"{self.account.hint()}")
            except smtplib.SMTPNotSupportedError as exc:
                raise MailError("server", f"{host} offers no way to sign in that "
                                          f"this agent speaks: {exc}")
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                raise MailError("network", f"{host} could not be spoken to: "
                                           f"{self._said(exc)}")
            self._hand_over(connection, raw, recipients)
        finally:
            try:
                connection.quit()
            except Exception:
                connection.close()

    def _hand_over(self, connection, raw: bytes, recipients: List[str]) -> None:
        try:
            code, said = connection.mail(self.account.email)
            if code != 250:
                raise MailError("server", f"The mail server refused the sender: "
                                          f"{self._said(said)}")
            refused = []
            for recipient in recipients:
                code, said = connection.rcpt(recipient)
                if code not in (250, 251):
                    refused.append(f"{recipient} ({self._said(said)})")
            if refused:
                connection.rset()
                raise MailError("server", "The mail server refused to send to "
                                          + ", ".join(refused) + ". Nothing was sent.")
        except (smtplib.SMTPServerDisconnected, OSError, socket.timeout) as exc:
            raise MailError("network", f"The mail server hung up before the "
                                       f"message was handed over: {self._said(exc)}")
        # From here the message is on its way to the server: an answer
        # that does not come is an outcome nobody knows.
        try:
            code, said = connection.data(raw)
        except (smtplib.SMTPServerDisconnected, OSError, socket.timeout) as exc:
            raise MailError("unknown", f"The message was handed to the mail server "
                                       f"and no answer came back: whether it was "
                                       f"sent is not known ({self._said(exc)}).")
        if code != 250:
            raise MailError("server", f"The mail server refused the message: "
                                      f"{self._said(said)}. Nothing was sent.")

    @staticmethod
    def _said(found) -> str:
        if isinstance(found, smtplib.SMTPResponseException):
            found = found.smtp_error
        if isinstance(found, bytes):
            found = found.decode("utf-8", "replace")
        return " ".join(str(found).split())[:200]
