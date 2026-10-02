"""A loopback web server for the web agents: pages, PDFs, feeds and
redirects, each set per test and changeable mid-test, over real HTTP.

The agents refuse loopback addresses unless DECENTAI_WEB_ALLOW_LOOPBACK
is 1 — a test-only escape that permits loopback and nothing else — so
``allow_loopback`` sets it for a test; workers inherit the environment
of the process that spawns them.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

import pytest

LOOPBACK_VARIABLE = "DECENTAI_WEB_ALLOW_LOOPBACK"


class WebStub:
    def __init__(self):
        self.routes: Dict[str, Tuple[int, Dict[str, str], bytes]] = {}
        self.hits: Dict[str, int] = {}
        self.user_agents: List[str] = []
        self._server: Optional[ThreadingHTTPServer] = None

    # -- what it serves ----------------------------------------------------
    def page(self, path: str, body, content_type: str = "text/html; charset=utf-8",
             status: int = 200) -> str:
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.routes[path] = (status, {"Content-Type": content_type}, raw)
        return self.url + path

    def redirect(self, path: str, location: str, status: int = 302) -> str:
        self.routes[path] = (status, {"Location": location}, b"")
        return self.url + path

    def fail(self, path: str, status: int = 503) -> str:
        self.routes[path] = (status, {"Content-Type": "text/plain"}, b"unavailable")
        return self.url + path

    # -- the server ----------------------------------------------------------
    def start(self) -> "WebStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                stub.hits[path] = stub.hits.get(path, 0) + 1
                stub.user_agents.append(self.headers.get("User-Agent") or "")
                status, headers, raw = stub.routes.get(
                    path, (404, {"Content-Type": "text/plain"}, b"not found"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv(LOOPBACK_VARIABLE, "1")
    stub = WebStub().start()
    yield stub
    stub.stop()


def text_pdf(pages: List[List[str]], title: str = "") -> bytes:
    """A small, valid PDF with a real text layer, one list of lines per
    page, written by hand (cross-reference table included) so the tests
    need no PDF-writing library."""
    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    count = len(pages)
    font_number = 3 + 2 * count
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               ("<< /Type /Pages /Kids [%s] /Count %d >>" % (
                   " ".join(f"{3 + 2 * i} 0 R" for i in range(count)), count)).encode()]
    for index, lines in enumerate(pages):
        content_number = 4 + 2 * index
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
                        f"/Contents {content_number} 0 R >>").encode())
        ops = ["BT", "/F1 12 Tf", "16 TL", "72 720 Td"]
        for line in lines:
            ops.append(f"({escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream
                       + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    info_number = None
    if title:
        objects.append(f"<< /Title ({escape(title)}) >>".encode("latin-1"))
        info_number = len(objects)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    info = f" /Info {info_number} 0 R" if info_number else ""
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info} >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)
