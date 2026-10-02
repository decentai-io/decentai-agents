"""Turning a stored file into pages of text, and saying which pages
could not be read.

A PDF page with no text layer — a scan, a picture, an empty page — is
``unreadable`` here, by number. It is never dropped: the caller sees
that page 3 of 7 said nothing to us, which is different from page 3
being blank.
"""

from __future__ import annotations

import base64
import csv
import io
from typing import Any, Dict, List, Tuple

MAX_PAGES = 400
MIN_TEXT_CHARS = 20   # fewer than this on a PDF page = no usable text layer

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Loaded:
    """One file, read: its kind, its pages of text, and what failed."""

    def __init__(self, file_ref: str, filename: str, kind: str,
                 pages: List[str], unreadable: List[int], headings=None,
                 problem: str = ""):
        self.file_ref = file_ref
        self.filename = filename
        self.kind = kind
        self.pages = pages                 # index 0 = page 1
        self.unreadable = unreadable       # 1-based page numbers
        self.headings = headings or []
        self.problem = problem

    @property
    def readable(self) -> List[int]:
        return [n for n in range(1, len(self.pages) + 1) if n not in self.unreadable]

    @property
    def characters(self) -> int:
        return sum(len(p) for p in self.pages)

    def text(self) -> str:
        return "\n\n".join(f"[page {n}]\n{p}" for n, p in enumerate(self.pages, 1) if p)


def kind_of(filename: str, file_type: str, raw: bytes) -> str:
    name = (filename or "").lower()
    mime = (file_type or "").lower()
    if raw[:5] == b"%PDF-" or name.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    if raw[:2] == b"PK" and (name.endswith(".docx") or mime == DOCX_MIME or b"word/" in raw[:4000]):
        return "docx"
    if name.endswith(".csv") or mime == "text/csv":
        return "csv"
    if name.endswith((".md", ".markdown")) or mime == "text/markdown":
        return "markdown"
    if name.endswith(".txt") or mime.startswith("text/"):
        return "text"
    try:
        raw.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "unsupported"


async def load(call, resource_id: str, file_ref: str) -> Loaded:
    record = await call.resources.read_file(resource_id, file_ref)
    filename = str(record.get("filename") or "")
    file_type = str(record.get("file_type") or record.get("mime_type") or "")
    if record.get("content_base64"):
        raw = base64.b64decode(record["content_base64"])
    else:
        raw = str(record.get("content") or "").encode("utf-8")
    kind = kind_of(filename, file_type, raw)
    if kind == "pdf":
        pages, unreadable, problem = _pdf(raw)
        return Loaded(file_ref, filename, kind, pages, unreadable, problem=problem)
    if kind == "docx":
        pages, headings, problem = _docx(raw)
        return Loaded(file_ref, filename, kind, pages, [], headings, problem)
    if kind == "unsupported":
        return Loaded(file_ref, filename, kind, [], [],
                      problem="Not a PDF, Word, text, Markdown or CSV file.")
    text = raw.decode("utf-8", "replace")
    if kind == "csv":
        text = _csv_text(text)
    return Loaded(file_ref, filename, kind, [text], [] if text.strip() else [1])


def _pdf(raw: bytes) -> Tuple[List[str], List[int], str]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return [], [], "The PDF is password-protected."
        count = len(reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        return [], [], f"The PDF could not be opened: {exc}"
    if count > MAX_PAGES:
        return [], [], f"The PDF has {count} pages; at most {MAX_PAGES} are read."
    pages, unreadable = [], []
    for number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if len(text) < MIN_TEXT_CHARS:
            unreadable.append(number)
            text = ""
        pages.append(text)
    return pages, unreadable, ""


def _docx(raw: bytes) -> Tuple[List[str], List[str], str]:
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = Document(io.BytesIO(raw))
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        return [], [], f"The Word file could not be opened: {exc}"
    lines, headings = [], []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        if paragraph.style is not None and str(paragraph.style.name).startswith(("Heading", "Title")):
            headings.append(text)
        lines.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return ["\n".join(lines)], headings, ""


def _csv_text(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows if any(row))


def paragraphs_of(loaded: Loaded) -> List[Tuple[int, str]]:
    """(page, line) pairs — the unit versions are compared by. A line,
    not a blank-line paragraph: a PDF's text layer rarely keeps the
    blank lines between paragraphs, and a Word file's paragraphs are
    already one line each here. Spacing is normalised."""
    out: List[Tuple[int, str]] = []
    for number, page in enumerate(loaded.pages, 1):
        for line in page.splitlines():
            line = " ".join(line.split())
            if line:
                out.append((number, line))
    return out
