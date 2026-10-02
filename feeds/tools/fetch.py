"""Fetching a public URL for the assistant — and only a public one.

An assistant that can be told "read this link" is a server-side request
forgery waiting to happen: the link can name the cloud's metadata
service, a database on the private network, or the platform itself.
So every fetch here, including every hop of a redirect:

- accepts only http and https, with no user name or password in it;
- resolves the host and refuses an address that is loopback, private,
  link-local, multicast, reserved, unspecified or otherwise not public
  (IPv4, IPv6, and IPv4 dressed as IPv6), saying which;
- checks the address the socket actually connected to as well, so a
  name that resolves to a public address for the check and a private
  one for the connection (DNS rebinding) is still refused;
- follows at most five redirects, by hand, re-checking each;
- stops reading past a size cap, and gives up on a slow server;
- ignores proxy settings from the environment, because a proxy would
  make the connected address the proxy's rather than the target's —
  all but the platform's own (see below);
- says who it is: a plain User-Agent naming DecentAI.

TEST-ONLY: ``DECENTAI_WEB_ALLOW_LOOPBACK=1`` in the environment permits
loopback addresses (127.0.0.0/8 and ::1) and nothing else — never a
private range. It exists so the tests can serve pages from a loopback
stub; a deployment must never set it.

BEHIND THE PLATFORM'S PROXY. Where the platform confines an agent, the
worker cannot look a name up or open a connection of its own: its one
way out is a proxy the platform runs, named in ``DECENTAI_PROXY``. That
proxy makes the same checks this file makes — every address a name
resolves to, and then a connection to the address it checked — so a
fetch goes through it and leaves the name to it. What this file still
does itself: the scheme, the credentials, an address written as an
address, and every hop of a redirect, each of which is a request the
proxy checks again.

This file is the same in every web agent of this repository: agents
cannot import one another, so each carries its own copy.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
import urllib.parse
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

LOOPBACK_VARIABLE = "DECENTAI_WEB_ALLOW_LOOPBACK"
#: The platform's proxy, where the platform confines this agent.
PROXY_VARIABLE = "DECENTAI_PROXY"
#: What marks an answer as the proxy's own and not the site's.
REFUSED_HEADER = "X-DecentAI-Refused"
#: What a client says of a tunnel the proxy refused: the proxy's
#: status, and its reason in its own words.
REFUSED_TUNNEL = re.compile(r"Tunnel connection failed: (\d{3}) (.*?)'\)")


class FetchError(Exception):
    """A fetch that did not produce a page, with a kind the assistant can
    act on: invalid_url, forbidden, too_many_redirects, timeout,
    network, http, too_large."""

    def __init__(self, kind: str, message: str, status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status

    def result(self) -> dict:
        out = {"error": str(self), "kind": self.kind}
        if self.status:
            out["status"] = self.status
        return out


class ForbiddenAddress(FetchError):
    def __init__(self, message: str):
        super().__init__("forbidden", message)


class AddressPolicy:
    """Which addresses a fetch may reach: public ones."""

    @staticmethod
    def refusal(address: str) -> str:
        """Why this address may not be fetched, or '' when it may."""
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return f"{address} is not an IP address"
        # ::ffff:10.0.0.1 is 10.0.0.1. Older Pythons do not look inside,
        # so unwrap it before asking anything.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            if os.environ.get(LOOPBACK_VARIABLE) == "1":
                return ""
            return f"{ip} is a loopback address (this machine)"
        if ip.is_unspecified:
            return f"{ip} is the unspecified address"
        if ip.is_link_local:
            return f"{ip} is a link-local address (such as a cloud metadata service)"
        if ip.is_multicast:
            return f"{ip} is a multicast address"
        if ip.is_reserved:
            return f"{ip} is a reserved address"
        if ip.is_private:
            return f"{ip} is a private network address"
        if not ip.is_global:
            return f"{ip} is not a public internet address"
        return ""


class _CheckedHTTPConnection(HTTPConnection):
    """Checks the peer after connecting: the name was checked when it was
    resolved, but it is resolved again to connect, and the two answers
    need not agree."""

    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _CheckedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


def _check_peer(sock) -> None:
    try:
        peer = sock.getpeername()[0]
    except OSError:
        return
    why = AddressPolicy.refusal(str(peer))
    if why:
        sock.close()
        raise ForbiddenAddress(f"Refused: the connection reached {why}.")


class _CheckedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _CheckedHTTPConnection


class _CheckedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _CheckedHTTPSConnection


class _CheckedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _CheckedHTTPPool, "https": _CheckedHTTPSPool}


class Fetched:
    """What came back: where it ended up, what it says it is, its bytes."""

    def __init__(self, url: str, final_url: str, status: int, content_type: str,
                 body: bytes, truncated: bool, redirects: List[str]):
        self.url = url
        self.final_url = final_url
        self.status = status
        self.content_type = content_type        # lower-case, no parameters
        self.header_charset = ""
        self.body = body
        self.truncated = truncated              # stopped reading at the cap
        self.redirects = redirects

    def text(self) -> str:
        """The body as text: the charset the server named, else one the
        page declares in its first bytes, else UTF-8 — undecodable bytes
        replaced rather than failing the whole page."""
        charset = self.header_charset or self._declared_charset() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def _declared_charset(self) -> str:
        head = self.body[:2048].decode("ascii", errors="replace").lower()
        for marker in ("charset=", "encoding="):
            at = head.find(marker)
            if at != -1:
                value = head[at + len(marker):].lstrip("\"' ")
                name = ""
                for ch in value:
                    if ch.isalnum() or ch in "-_":
                        name += ch
                    else:
                        break
                if name:
                    return name
        return ""


class SafeFetcher:
    MAX_BYTES = 5 * 1024 * 1024
    MAX_REDIRECTS = 5
    CONNECT_SECONDS = 10
    READ_SECONDS = 20
    TOTAL_SECONDS = 45          # a trickling server cannot hold a call forever
    REDIRECT_STATUSES = (301, 302, 303, 307, 308)

    def __init__(self, user_agent: str, max_bytes: Optional[int] = None):
        self.user_agent = user_agent
        self.max_bytes = int(max_bytes or self.MAX_BYTES)

    def get(self, url: str, accept: str = "*/*") -> Fetched:
        original = url
        redirects: List[str] = []
        proxy = os.environ.get(PROXY_VARIABLE) or ""
        session = requests.Session()
        session.trust_env = False
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        else:
            session.mount("http://", _CheckedAdapter())
            session.mount("https://", _CheckedAdapter())
        try:
            while True:
                if proxy:
                    self.check_url(url, resolve=False)
                else:
                    self.check_url(url)
                try:
                    response = session.get(
                        url, stream=True, allow_redirects=False,
                        timeout=(self.CONNECT_SECONDS, self.READ_SECONDS),
                        headers={"User-Agent": self.user_agent, "Accept": accept})
                except ForbiddenAddress:
                    raise
                except requests.exceptions.Timeout:
                    raise FetchError("timeout", f"{self._host(url)} did not answer in time.")
                except requests.exceptions.ProxyError as exc:
                    refused = REFUSED_TUNNEL.search(str(exc))
                    if refused:
                        raise self._refused(url, int(refused.group(1)), refused.group(2))
                    raise FetchError("network", f"{self._host(url)} could not be reached "
                                                f"through the platform's proxy.")
                except requests.exceptions.RequestException as exc:
                    raise FetchError("network", f"{self._host(url)} could not be reached: "
                                                f"{self._short(exc)}")
                with response:
                    if proxy and response.headers.get(REFUSED_HEADER):
                        raise self._refused(url, response.status_code,
                                            response.reason or "")
                    if response.status_code in self.REDIRECT_STATUSES:
                        location = response.headers.get("Location") or ""
                        if not location:
                            raise FetchError("http", f"{url} redirected without saying where.",
                                             response.status_code)
                        if len(redirects) >= self.MAX_REDIRECTS:
                            raise FetchError("too_many_redirects",
                                             f"{original} redirected more than "
                                             f"{self.MAX_REDIRECTS} times; not followed further.")
                        url = urllib.parse.urljoin(url, location)
                        redirects.append(url)
                        continue
                    if response.status_code >= 400:
                        raise FetchError("http", f"{url} answered HTTP {response.status_code}.",
                                         response.status_code)
                    body, truncated = self._read(response, url)
                    raw_type = response.headers.get("Content-Type") or ""
                    fetched = Fetched(original, url, response.status_code,
                                      raw_type.split(";", 1)[0].strip().lower(),
                                      body, truncated, redirects)
                    for part in raw_type.split(";")[1:]:
                        name, _, value = part.partition("=")
                        if name.strip().lower() == "charset":
                            fetched.header_charset = value.strip().strip("\"'")
                    return fetched
        finally:
            session.close()

    def _refused(self, url: str, status: int, why: str) -> FetchError:
        """What the platform's proxy refused, in this file's own kinds:
        an address it will not reach is forbidden, and a host it could
        not reach is the network's."""
        host = self._host(url)
        why = str(why or "").strip().rstrip(".") or "the platform's proxy refused it"
        if status == 403:
            return ForbiddenAddress(f"Refused to fetch {host}: {why}.")
        return FetchError("network", f"{host} could not be reached: {why}.")

    def check_url(self, url: str, resolve: bool = True) -> None:
        """Refuse a URL before any byte is sent: its scheme, its
        credentials, and every address its host resolves to. With
        ``resolve`` off the name is left to the platform's proxy, and
        only an address written as an address is checked here."""
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise FetchError("invalid_url", f"{url!r} is not a valid URL: {exc}")
        if parts.scheme.lower() not in ("http", "https"):
            raise FetchError("invalid_url", f"Only http and https URLs can be fetched, "
                                            f"not {parts.scheme or 'a URL without a scheme'}.")
        if parts.username is not None or parts.password is not None:
            raise FetchError("invalid_url", "A URL with a user name or password in it "
                                            "is not fetched.")
        host = parts.hostname or ""
        if not host:
            raise FetchError("invalid_url", f"{url!r} names no host.")
        port = port or (443 if parts.scheme.lower() == "https" else 80)
        if not resolve:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return
            why = AddressPolicy.refusal(host)
            if why:
                raise ForbiddenAddress(f"Refused to fetch {host}: {why}. Only public "
                                       f"internet addresses are fetched.")
            return
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise FetchError("network", f"{host} could not be resolved: {exc}")
        addresses = sorted({str(answer[4][0]) for answer in answers})
        if not addresses:
            raise FetchError("network", f"{host} resolved to no address.")
        # One forbidden answer refuses the host: which one a connection
        # would use is not ours to choose.
        for address in addresses:
            why = AddressPolicy.refusal(address)
            if why:
                raise ForbiddenAddress(f"Refused to fetch {host}: {why}. Only public "
                                       f"internet addresses are fetched.")

    def _read(self, response, url: str):
        chunks, size, started = [], 0, time.monotonic()
        try:
            for chunk in response.iter_content(64 * 1024):
                if time.monotonic() - started > self.TOTAL_SECONDS:
                    raise FetchError("timeout", f"{self._host(url)} was too slow to send the page.")
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_bytes:
                    return b"".join(chunks)[: self.max_bytes], True
        except requests.exceptions.RequestException as exc:
            raise FetchError("network", f"Reading {url} failed: {self._short(exc)}")
        return b"".join(chunks), False

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urllib.parse.urlsplit(url).hostname or url
        except ValueError:
            return url

    @staticmethod
    def _short(exc: Exception) -> str:
        text = str(exc)
        # The proxy's address carries this worker's pass to it, which is
        # nobody's to read in an error.
        proxy = os.environ.get(PROXY_VARIABLE) or ""
        if proxy:
            text = text.replace(proxy, "the platform's proxy")
        return text if len(text) <= 200 else text[:200] + "…"
