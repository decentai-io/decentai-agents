"""The attempts themselves: a connection opened and closed, and what
became of it.

    through the proxy   where the platform confines agents, a worker is
                        pointed at the platform's proxy and handed a
                        pass. The proxy is asked for a tunnel to the
                        host, and its answer is the outcome: it opens
                        what the agent declared and refuses the rest,
                        saying why.
    past the proxy      a connection made straight to an address, and a
                        name looked up: what a worker could always do
                        before anything held it.

Four outcomes, and they are different things:

    reached       the connection was opened
    refused       the platform stopped it, and ``reason`` is its own
    unreachable   it was allowed, and the host did not answer
    not_tried     this agent would not try it
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
from typing import Optional, Tuple
from urllib.parse import urlsplit

#: Where the platform points a confined worker.
PROXY_VARIABLE = "DECENTAI_PROXY"
#: The web agents' escape for tests, honoured here as they honour it:
#: this machine's own addresses may be tried.
LOOPBACK_VARIABLE = "DECENTAI_WEB_ALLOW_LOOPBACK"
#: For this agent's own tests, which have no internet to try: the port
#: a named host is tried on, what stands for the address and the
#: name tried past the proxy, and where a name is found when nothing
#: looks one up. JSON: {"port": 8443, "direct": "127.0.0.1:8443",
#: "name": "localhost", "resolve": {"example.com": "127.0.0.1"}}.
TEST_VARIABLE = "DECENTAI_AGENT_CONNECTION_CHECK_TEST"

HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")

REACHED, REFUSED, UNREACHABLE, NOT_TRIED = (
    "reached", "refused", "unreachable", "not_tried")


class NotAnAddress(ValueError):
    """What the person named is not a host this agent can try."""


class Outcome:
    def __init__(self, outcome: str, reason: str = ""):
        self.outcome = outcome
        self.reason = reason


class Attempts:
    SECONDS = 10

    def __init__(self):
        self.proxy = urlsplit(os.environ.get(PROXY_VARIABLE) or "")
        try:
            self.test = json.loads(os.environ.get(TEST_VARIABLE) or "{}")
        except ValueError:
            self.test = {}

    # ------------------------------------------------------------------
    # What is tried
    # ------------------------------------------------------------------

    @property
    def through_the_proxy(self) -> bool:
        """Whether this worker was pointed at the platform's proxy."""
        return bool(self.proxy.hostname and self.proxy.port)

    @property
    def port(self) -> int:
        """The port a named host is tried on: where a service answers."""
        return int(self.test.get("port") or 443)

    @property
    def direct(self) -> Tuple[str, int]:
        """An address on the internet, written as one: no name has to
        be looked up to try it."""
        host, _, port = str(self.test.get("direct") or "1.1.1.1:443").rpartition(":")
        return host, int(port)

    @property
    def name(self) -> str:
        return str(self.test.get("name") or "example.com")

    @staticmethod
    def where(address: str) -> Tuple[str, Optional[int]]:
        """(host, port) of what a person typed: a name, a name and a
        port, or a whole address. The port is None where none was
        said."""
        address = str(address or "").strip()
        if not address:
            raise NotAnAddress("No address was given.")
        scheme = ""
        if "://" in address:
            scheme = address.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                raise NotAnAddress(
                    f"{scheme}:// is not tried; name a host, or an http or https address.")
        try:
            parts = urlsplit(address if scheme else "//" + address)
            host, port = (parts.hostname or "").lower().rstrip("."), parts.port
        except ValueError:
            raise NotAnAddress(f"'{address}' is not an address this agent can read.")
        if parts.username or parts.password:
            raise NotAnAddress("An address with a name and a password in it is not tried.")
        if not host or not (HOST_RE.match(host) or Attempts.is_address(host)):
            raise NotAnAddress(f"'{address}' names no host.")
        if port is None and scheme == "http":
            port = 80
        return host, port

    @staticmethod
    def is_address(host: str) -> bool:
        try:
            ipaddress.ip_address(host.strip("[]"))
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Trying
    # ------------------------------------------------------------------

    def reach(self, host: str, port: int) -> Outcome:
        """The host, the way this agent is given to reach anything:
        through the platform's proxy where there is one."""
        if self.through_the_proxy:
            return self.by_the_proxy(host, port)
        return self.unheld(host, port)

    def by_the_proxy(self, host: str, port: int) -> Outcome:
        """Ask the proxy for a tunnel. Its first line is the answer,
        and for a refusal the reason is on it, in the platform's own
        words."""
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        credentials = base64.b64encode(
            f"{self.proxy.username or ''}:{self.proxy.password or ''}".encode()).decode()
        try:
            with socket.create_connection(
                    (self.proxy.hostname, self.proxy.port), self.SECONDS) as link:
                link.settimeout(self.SECONDS * 3)
                link.sendall((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                              f"Proxy-Authorization: Basic {credentials}\r\n\r\n").encode())
                answer = b""
                while b"\r\n" not in answer and len(answer) < 4096:
                    chunk = link.recv(4096)
                    if not chunk:
                        break
                    answer += chunk
        except OSError as exc:
            return Outcome(UNREACHABLE,
                           f"The platform's proxy did not answer: {self.short(exc)}")
        line = answer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        found = re.match(r"^HTTP/\d\.\d (\d{3}) ?(.*)$", line)
        if not found:
            return Outcome(UNREACHABLE, "The platform's proxy gave no answer that can be read.")
        status, reason = int(found.group(1)), found.group(2).strip()
        if status == 200:
            return Outcome(REACHED)
        if status in (502, 504):
            return Outcome(UNREACHABLE, reason or "The host did not answer.")
        if status == 407:
            return Outcome(REFUSED, "The platform's proxy does not know this worker.")
        return Outcome(REFUSED, reason or f"The platform's proxy answered {status}.")

    def unheld(self, host: str, port: int) -> Outcome:
        """No proxy: the connection is made straight from here, as any
        program would make it. An address inside the network is not
        tried: nothing here would have stopped it, and that is not a
        reason to knock on it."""
        known = (self.test.get("resolve") or {}).get(host)
        try:
            found = [(None, None, None, None, (known, port))] if known else (
                socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            return Outcome(UNREACHABLE, f"{host} was not found: {self.short(exc)}")
        addresses = sorted({entry[4][0] for entry in found})
        inside = [a for a in addresses if not self.public(a)]
        if inside:
            return Outcome(
                NOT_TRIED,
                f"{host} is an address inside the network ({inside[0]}). Nothing "
                f"here would have refused it; this agent does not try one.")
        return self.straight(addresses[0], port)

    def straight(self, address: str, port: int) -> Outcome:
        """A connection to an address, asking nobody."""
        try:
            socket.create_connection((address, port), self.SECONDS).close()
        except OSError as exc:
            return Outcome(self.kind_of(exc), self.short(exc))
        return Outcome(REACHED)

    def looked_up(self, name: str) -> Outcome:
        """A name looked up from here. A worker that is held cannot
        look one up: the proxy does, for the hosts it opens."""
        try:
            socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            return Outcome(REFUSED, self.short(exc))
        return Outcome(REACHED)

    # ------------------------------------------------------------------
    def public(self, address: str) -> bool:
        found = ipaddress.ip_address(address)
        if found.is_loopback and os.environ.get(LOOPBACK_VARIABLE) == "1":
            return True
        return found.is_global

    @staticmethod
    def kind_of(exc: OSError) -> str:
        """Refused by what holds the worker, or not answered by the
        host. A firewall that rejects says so at once: no route, not
        permitted, refused. A host that is only away says nothing until
        the time is up."""
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return UNREACHABLE
        return REFUSED

    @staticmethod
    def short(exc: Exception) -> str:
        said = str(getattr(exc, "strerror", "") or exc).strip()
        return (said or type(exc).__name__)[:160]
