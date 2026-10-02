"""Reading RSS 2.0, RSS 1.0 (RDF) and Atom 1.0 with the standard library,
and finding the feed a web page advertises.

Namespaces are dropped to local names before anything is looked up:
the three formats spell the same ideas in different namespaces (and
RSS 2.0 in none), and a feed that borrows Dublin Core's ``dc:date`` or
``dc:creator`` is still read.

Dates are normalised to UTC ``YYYY-MM-DDTHH:MM:SSZ`` so that comparing
two is comparing two strings. A date that cannot be read is no date:
the item is undated, not guessed.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import List, Optional

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")
SUMMARY_CHARS = 300
MAX_ITEMS = 500
MAX_ID_CHARS = 300


class FeedProblem(Exception):
    pass


class Item:
    def __init__(self, item_id: str, title: str, link: str, published: str,
                 summary: str, author: str):
        self.id = item_id
        self.title = title
        self.link = link
        self.published = published      # normalised UTC, or ""
        self.summary = summary
        self.author = author

    def row(self) -> dict:
        return {"id": self.id, "title": self.title, "link": self.link,
                "published": self.published, "summary": self.summary,
                "author": self.author}


class Feed:
    def __init__(self, url: str, kind: str, title: str, items: List[Item]):
        self.url = url
        self.format = kind               # rss | atom | rdf
        self.title = title
        self.items = items


# -- dates ------------------------------------------------------------------

def normalise_date(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    parsed: Optional[datetime] = None
    try:
        parsed = parsedate_to_datetime(text)            # RFC 822 / 2822
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        iso = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)        # ISO 8601
        except ValueError:
            match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)"
                             r"(?:\.\d+)?([+-]\d{2}:?\d{2})?$", iso)
            if not match:
                return ""
            zone = (match.group(3) or "+00:00")
            if ":" not in zone:
                zone = zone[:3] + ":" + zone[3:]
            seconds = match.group(2) if match.group(2).count(":") == 2 else match.group(2) + ":00"
            try:
                parsed = datetime.fromisoformat(f"{match.group(1)}T{seconds}{zone}")
            except ValueError:
                return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- parsing ----------------------------------------------------------------

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element, *names):
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def _text(element, *names) -> str:
    child = _child(element, *names)
    if child is None:
        return ""
    return " ".join("".join(child.itertext()).split())


def looks_like_feed(body: bytes) -> bool:
    head = body[:1024].lstrip().lower()
    return head.startswith(b"<?xml") or head.startswith((b"<rss", b"<feed", b"<rdf"))


def parse_feed(body: bytes, url: str) -> Feed:
    # A feed has no business declaring entities, and refusing them keeps
    # entity-expansion tricks out of the parser entirely.
    if b"<!ENTITY" in body[:65536].upper():
        raise FeedProblem(f"{url} declares XML entities; it is not read.")
    try:
        root = ElementTree.fromstring(body.lstrip())
    except ElementTree.ParseError as exc:
        raise FeedProblem(f"{url} is not a readable feed: {exc}")
    name = _local(root.tag)
    if name == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedProblem(f"{url} is RSS without a channel.")
        entries = [c for c in channel if _local(c.tag) == "item"]
        return Feed(url, "rss", _text(channel, "title"),
                    [_rss_item(e, url) for e in entries[:MAX_ITEMS]])
    if name == "RDF":
        channel = _child(root, "channel")
        title = _text(channel, "title") if channel is not None else ""
        entries = [c for c in root if _local(c.tag) == "item"]
        return Feed(url, "rdf", title, [_rss_item(e, url) for e in entries[:MAX_ITEMS]])
    if name == "feed":
        entries = [c for c in root if _local(c.tag) == "entry"]
        return Feed(url, "atom", _text(root, "title"),
                    [_atom_item(e, url) for e in entries[:MAX_ITEMS]])
    raise FeedProblem(f"{url} is XML but not RSS or Atom (its root is <{name}>).")


def _rss_item(entry, base: str) -> Item:
    link = urllib.parse.urljoin(base, _text(entry, "link")) if _text(entry, "link") else ""
    about = ""
    for key, value in entry.attrib.items():
        if _local(key) == "about":
            about = value
    guid = _text(entry, "guid") or about
    published = normalise_date(_text(entry, "pubDate") or _text(entry, "date")
                               or _text(entry, "published") or _text(entry, "updated"))
    summary = _text(entry, "description") or _text(entry, "encoded")
    author = _text(entry, "author") or _text(entry, "creator")
    title = _text(entry, "title")
    return Item(_identity(guid, link, title, summary), title, link, published,
                _plain(summary), author)


def _atom_item(entry, base: str) -> Item:
    link = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate" and child.attrib.get("href"):
            link = urllib.parse.urljoin(base, child.attrib["href"])
            break
        if not link and child.attrib.get("href"):
            link = urllib.parse.urljoin(base, child.attrib["href"])
    published = normalise_date(_text(entry, "published") or _text(entry, "updated")
                               or _text(entry, "date"))
    summary = _text(entry, "summary") or _text(entry, "content")
    author_element = _child(entry, "author")
    author = _text(author_element, "name") if author_element is not None else ""
    title = _text(entry, "title")
    return Item(_identity(_text(entry, "id"), link, title, summary), title, link,
                published, _plain(summary), author)


def _identity(item_id: str, link: str, title: str, summary: str) -> str:
    """guid or id, else link, else a digest of the words — and never so
    long that fifty of them outgrow a record."""
    ident = item_id or link
    if not ident:
        ident = "sha1:" + hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()
    if len(ident) > MAX_ID_CHARS:
        ident = "sha1:" + hashlib.sha1(ident.encode("utf-8")).hexdigest()
    return ident


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _plain(markup: str) -> str:
    """A summary as plain words, clipped: descriptions are often HTML."""
    if "<" in markup:
        parser = _Plain()
        try:
            parser.feed(markup)
            parser.close()
        except Exception:
            pass
        markup = "".join(parser.parts)
    text = " ".join(markup.split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS].rstrip() + "…"


# -- discovery --------------------------------------------------------------

class _FeedLinks(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.feeds: List[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and attributes.get("href"):
            self.base = urllib.parse.urljoin(self.base, attributes["href"])
        if tag != "link":
            return
        rels = attributes.get("rel", "").lower().split()
        kind = attributes.get("type", "").lower().split(";")[0].strip()
        if "alternate" in rels and kind in FEED_TYPES and attributes.get("href"):
            url = urllib.parse.urljoin(self.base, attributes["href"].strip())
            if url not in self.feeds:
                self.feeds.append(url)


def advertised_feeds(markup: str, base: str) -> List[str]:
    parser = _FeedLinks(base)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    return parser.feeds
