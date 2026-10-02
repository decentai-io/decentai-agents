"""Turning fetched bytes into readable text in numbered parts.

HTML is read with the standard library's parser: scripts, styles,
navigation, footers and asides are dropped, headings become their own
lines (Markdown-style, ``## Heading``) and start a new section, list
items keep a dash, and links are collected with absolute URLs. It is a
best effort, not a browser: a page that builds its text with JavaScript
reads as nearly empty, and that is said rather than hidden.

A PDF is read per page with pypdf; a page with no text layer is listed
as unreadable, never dropped. Plain text and JSON pass through.

An HTML section or a text body is split at line boundaries into parts of
at most PART_CHARS that keep its heading, so paging by part never has
to cut text off. A PDF part is one page, whatever its length, so that a
part number is a page number.

This file is the same in every web agent that reads pages.
"""

from __future__ import annotations

import io
import re
import urllib.parse
from html.parser import HTMLParser
from typing import List, Optional, Tuple

PART_CHARS = 4000
MAX_LINKS_KEPT = 500
MAX_PDF_PAGES = 400

TEXT_TYPES = ("application/json", "application/xml", "application/javascript")


class Part:
    def __init__(self, number: int, heading: str, text: str):
        self.number = number
        self.heading = heading
        self.text = text

    def row(self) -> dict:
        return {"number": self.number, "heading": self.heading, "text": self.text}


class Page:
    """A page as text: its kind, title, parts and links."""

    def __init__(self, kind: str, unit: str, title: str = ""):
        self.kind = kind                    # html | pdf | text | json
        self.unit = unit                    # section | page | part
        self.title = title
        self.parts: List[Part] = []
        self.links: List[dict] = []
        self.unreadable_pages: List[int] = []

    @property
    def characters(self) -> int:
        return sum(len(p.text) for p in self.parts)

    def lines(self) -> List[str]:
        """Every line of readable text, headings included, whitespace
        collapsed — the form a watch compares."""
        out = []
        seen_heading = None
        for part in self.parts:
            if part.heading and part.heading != seen_heading:
                out.append(part.heading)
                seen_heading = part.heading
            for line in part.text.split("\n"):
                line = " ".join(line.split())
                if line:
                    out.append(line)
        return out

    def markdown(self) -> str:
        blocks, seen_heading = [], None
        for part in self.parts:
            if part.heading and part.heading != seen_heading:
                blocks.append(part.heading)
                seen_heading = part.heading
            if part.text:
                blocks.append(part.text)
        return "\n\n".join(blocks)


class UnsupportedContent(Exception):
    pass


def kind_of(content_type: str, body: bytes) -> str:
    head = body[:512].lstrip().lower()
    if body[:5] == b"%PDF-" or content_type == "application/pdf":
        return "pdf"
    if content_type in ("text/html", "application/xhtml+xml") or \
            head.startswith((b"<!doctype html", b"<html")):
        return "html"
    if content_type == "application/json" or content_type.endswith("+json"):
        return "json"
    if content_type.startswith("text/") or content_type in TEXT_TYPES or \
            content_type.endswith("+xml"):
        return "text"
    if not content_type:
        try:
            body[:4096].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "unsupported"


def read_page(fetched) -> Page:
    kind = kind_of(fetched.content_type, fetched.body)
    if kind == "pdf":
        return _pdf(fetched)
    if kind == "html":
        return _html(fetched.text(), fetched.final_url)
    if kind in ("text", "json"):
        page = Page(kind, "part")
        _add_parts(page, "", fetched.text().replace("\r\n", "\n"))
        return page
    raise UnsupportedContent(
        f"{fetched.final_url} is {fetched.content_type or 'of an unknown type'}, "
        f"which cannot be read as text.")


# -- splitting ------------------------------------------------------------

def _add_parts(page: Page, heading: str, text: str) -> None:
    text = text.strip("\n")
    if not text and not heading:
        return
    for chunk in _chunks(text):
        page.parts.append(Part(len(page.parts) + 1, heading, chunk))


def _chunks(text: str) -> List[str]:
    if len(text) <= PART_CHARS:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > PART_CHARS:          # one enormous line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:PART_CHARS])
            line = line[PART_CHARS:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > PART_CHARS:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# -- PDF ------------------------------------------------------------------

def _pdf(fetched) -> Page:
    if fetched.truncated:
        raise UnsupportedContent(
            f"The PDF at {fetched.final_url} is larger than "
            f"{len(fetched.body) // (1024 * 1024)} MB and was not read.")
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(fetched.body))
        pages = reader.pages
        count = len(pages)
    except Exception as exc:
        raise UnsupportedContent(f"The PDF at {fetched.final_url} could not be opened: {exc}")
    title = ""
    try:
        title = str((reader.metadata or {}).get("/Title") or "")
    except Exception:
        title = ""
    page = Page("pdf", "page", title)
    for number in range(1, min(count, MAX_PDF_PAGES) + 1):
        try:
            text = pages[number - 1].extract_text() or ""
        except Exception:
            text = ""
        text = "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()
        if not text:
            page.unreadable_pages.append(number)
        # One part per page, numbered by page: a long page is clipped
        # rather than renumbering every page after it.
        page.parts.append(Part(number, "", text[: PART_CHARS * 5]))
    return page


# -- HTML -----------------------------------------------------------------

class _Reader(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer",
            "aside", "iframe", "canvas", "select", "button"}
    SKIP_ROLES = {"navigation", "contentinfo", "complementary", "banner", "search"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "br", "tr", "table",
             "ul", "ol", "li", "blockquote", "pre", "dd", "dt", "dl", "figure",
             "figcaption", "hr", "form", "fieldset", "address", "details", "summary"}
    HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "source", "track", "wbr"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.blocks: List[Tuple[int, str]] = []   # (heading level or 0, text)
        self.links: List[dict] = []
        self._link_urls = set()
        self._skip_stack: List[str] = []
        self._in_title = False
        self._heading = 0
        self._buffer: List[str] = []
        self._pre = 0
        self._anchor: Optional[dict] = None

    # the parser's callbacks
    def handle_starttag(self, tag, attrs):
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if self._skip_stack:
            if tag not in self.VOID:
                self._skip_stack.append(tag)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "base" and attributes.get("href"):
            self.base_url = urllib.parse.urljoin(self.base_url, attributes["href"])
            return
        if tag in self.SKIP or attributes.get("role", "").lower() in self.SKIP_ROLES \
                or "hidden" in attributes or attributes.get("aria-hidden") == "true":
            if tag not in self.VOID:
                self._skip_stack.append(tag)
            return
        if tag in self.HEADINGS:
            self._flush()
            self._heading = self.HEADINGS[tag]
        elif tag in self.BLOCK:
            self._flush()
            if tag == "li":
                self._buffer.append("- ")
            if tag == "pre":
                self._pre += 1
        elif tag in ("td", "th"):
            self._buffer.append(" | ")
        if tag == "a" and attributes.get("href"):
            self._anchor = {"href": attributes["href"], "text": []}

    def handle_endtag(self, tag):
        if self._skip_stack:
            # Close back to the matching opener; a stray end tag inside a
            # skipped element is ignored rather than ending the skip.
            if tag in self._skip_stack:
                while self._skip_stack and self._skip_stack.pop() != tag:
                    pass
            return
        if tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS:
            self._flush()
            self._heading = 0
        elif tag in self.BLOCK:
            self._flush()
            if tag == "pre" and self._pre:
                self._pre -= 1
        if tag == "a" and self._anchor is not None:
            self._keep_link(self._anchor)
            self._anchor = None

    def handle_data(self, data):
        if self._in_title and not self._skip_stack:
            self.title += data
            return
        if self._skip_stack:
            return
        self._buffer.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def close(self):
        super().close()
        self._flush()

    # helpers
    def _flush(self):
        raw = "".join(self._buffer)
        self._buffer = []
        if self._pre:
            lines = [line.rstrip() for line in raw.split("\n")]
            text = "\n".join(lines).strip("\n")
        else:
            text = " ".join(raw.split())
            text = text.strip(" |")
        if text and text != "-":
            self.blocks.append((self._heading, text))

    def _keep_link(self, anchor):
        if len(self.links) >= MAX_LINKS_KEPT:
            return
        url = urllib.parse.urljoin(self.base_url, anchor["href"].strip())
        url = urllib.parse.urldefrag(url)[0]
        if not url.lower().startswith(("http://", "https://")) or url in self._link_urls:
            return
        self._link_urls.add(url)
        text = " ".join("".join(anchor["text"]).split())[:200]
        self.links.append({"text": text, "url": url})


def _html(markup: str, base_url: str) -> Page:
    reader = _Reader(base_url)
    try:
        reader.feed(markup)
        reader.close()
    except Exception:
        # html.parser is forgiving; whatever it managed before giving up
        # is still the page's text.
        reader._flush()
    page = Page("html", "section", " ".join(reader.title.split()))
    heading, lines = "", []
    for level, text in reader.blocks:
        if level:
            _add_parts(page, heading, "\n".join(lines))
            heading, lines = "#" * level + " " + text, []
        else:
            lines.append(text)
    _add_parts(page, heading, "\n".join(lines))
    page.links = reader.links
    return page


def normalized(text: str) -> str:
    return " ".join(text.split())


def phrase_pattern(phrase: str):
    """A phrase as a case-insensitive pattern that matches across any run
    of whitespace, so a line break in the page does not hide it."""
    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"\s+".join(words), re.IGNORECASE)
