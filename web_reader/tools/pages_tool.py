"""Reading a public web page or PDF the way Documents reads a file: the
words in numbered parts with a total, what a phrase's passages say and
where, and a copy saved for another agent to read.

Nothing here is kept between calls: every function fetches afresh, so
what it quotes is what the page says now.
"""

import base64
import re
import urllib.parse
from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .fetch import FetchError, SafeFetcher
from .page_text import UnsupportedContent, phrase_pattern, read_page

USER_AGENT = "DecentAI-WebReader/0.1 (reads a page a DecentAI user asked for)"

LINKS_SHOWN = 25
QUOTE_CONTEXT = 150
MATCHES_PER_PHRASE = 10
MATCHES_SHOWN = 25
# What the platform stores, and what the manifest's file slot declares.
MAX_SAVE_BYTES = 25 * 1024 * 1024


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PagesTool(ToolBase):
    id = "pages"

    async def read(self, call):
        url = str(call.inputs["url"]).strip()
        loaded, failure = await self._load(call, url)
        if failure:
            return failure, "error"
        fetched, page, fetched_at = loaded
        total = len(page.parts)
        start = int(call.inputs.get("from") or 1)
        budget = int(call.inputs.get("max_chars") or 20000)
        if total and start > total:
            return {"error": f"The page has {total} {page.unit}s; there is no "
                             f"{page.unit} {start}.", "kind": "invalid"}, "error"
        parts, used, truncated, next_number = [], 0, False, 0
        for part in page.parts[start - 1:]:
            if parts and used + len(part.text) > budget:
                truncated, next_number = True, part.number
                break
            row = part.row()
            if len(part.text) > budget:
                # Only a PDF page can outgrow the budget on its own.
                row["text"] = part.text[:budget]
                row["clipped"] = True
            parts.append(row)
            used += len(row["text"])
        result = {
            "url": url, "final_url": fetched.final_url, "title": page.title,
            "content_type": fetched.content_type, "kind": page.kind, "unit": page.unit,
            "parts": parts, "total_parts": total, "total_chars": page.characters,
            "truncated": truncated, "unreadable_pages": page.unreadable_pages,
            "links": page.links[:LINKS_SHOWN], "total_links": len(page.links),
            "fetched_at": fetched_at, "body_truncated": fetched.truncated,
        }
        if truncated:
            result["next"] = next_number
        note = self._note(fetched, page)
        if note:
            result["note"] = note
        return result, "success"

    async def find(self, call):
        url = str(call.inputs["url"]).strip()
        phrases = [str(p).strip() for p in (call.inputs.get("phrases") or [])]
        if str(call.inputs.get("phrase") or "").strip():
            phrases.insert(0, str(call.inputs["phrase"]).strip())
        phrases = list(dict.fromkeys(p for p in phrases if p))
        if not phrases:
            return {"error": "Give a phrase, or phrases, to look for.",
                    "kind": "invalid"}, "error"
        loaded, failure = await self._load(call, url)
        if failure:
            return failure, "error"
        fetched, page, fetched_at = loaded
        summary, matches, total_matches = [], [], 0
        for phrase in phrases:
            pattern = phrase_pattern(phrase)
            count, kept = 0, 0
            for part in page.parts:
                for match in pattern.finditer(part.text):
                    count += 1
                    if kept < MATCHES_PER_PHRASE:
                        kept += 1
                        matches.append({"phrase": phrase, "number": part.number,
                                        "heading": part.heading,
                                        "quote": self._quote(part.text, match)})
            total_matches += count
            summary.append({"phrase": phrase, "found": count > 0, "count": count})
        absent = [row["phrase"] for row in summary if not row["found"]]
        result = {
            "url": url, "final_url": fetched.final_url, "title": page.title,
            "unit": page.unit, "phrases": summary, "absent": absent,
            "matches": matches[:MATCHES_SHOWN], "total_matches": total_matches,
            "more": len(matches) > MATCHES_SHOWN or total_matches > len(matches),
            "unreadable_pages": page.unreadable_pages, "fetched_at": fetched_at,
        }
        if absent:
            result["note"] = ("Not on the page: " + "; ".join(f"“{p}”" for p in absent)
                              + ". Say so; do not supply it from elsewhere.")
        return result, "success"

    async def save(self, call):
        url = str(call.inputs["url"]).strip()
        try:
            fetched = SafeFetcher(USER_AGENT).get(url)
        except FetchError as exc:
            return exc.result(), "error"
        fetched_at = now_utc()
        wanted = str(call.inputs.get("filename") or "").strip()
        if fetched.body[:5] == b"%PDF-" or fetched.content_type == "application/pdf":
            if fetched.truncated:
                return self._too_large(url, len(fetched.body), exact=False)
            if fetched.body[:5] != b"%PDF-":
                return {"error": f"{fetched.final_url} says it is a PDF but is not one.",
                        "kind": "unsupported"}, "error"
            # The original bytes: Documents reads them page by page itself.
            raw, suffix, mime, kind = fetched.body, ".pdf", "application/pdf", "pdf"
            title = ""
        else:
            try:
                page = read_page(fetched)
            except UnsupportedContent as exc:
                return {"error": str(exc), "kind": "unsupported"}, "error"
            header = f"Source: {fetched.final_url}\nFetched: {fetched_at}\n"
            if fetched.truncated:
                header += (f"Note: only the first {len(fetched.body):,} bytes of the "
                           f"page were read.\n")
            if page.kind == "html":
                title = page.title
                heading = f"# {title}\n\n" if title else ""
                text = f"{heading}{header}\n---\n\n{page.markdown()}\n"
                suffix, mime = ".md", "text/markdown"
            else:
                title = ""
                text = f"{header}\n{page.markdown()}\n"
                suffix, mime = ".txt", "text/plain"
            raw, kind = text.encode("utf-8"), page.kind
        if len(raw) > MAX_SAVE_BYTES:
            return self._too_large(url, len(raw), exact=True)
        filename = self._filename(wanted, title, fetched.final_url, suffix)
        await call.progress(f"Saving {filename} ({len(raw):,} bytes)")
        saved = await call.resources.create_file(
            "page", filename, content_base64=base64.b64encode(raw).decode("ascii"))
        return {"file_ref": saved["resource_ref"], "filename": filename,
                "size": len(raw), "mime_type": mime, "kind": kind,
                "source_url": fetched.final_url, "fetched_at": fetched_at}, "success"

    # ------------------------------------------------------------------
    @staticmethod
    async def _load(call, url):
        """(fetched, page, fetched_at), None — or None, an error result."""
        await call.progress(f"Fetching {url}")
        try:
            fetched = SafeFetcher(USER_AGENT).get(url)
        except FetchError as exc:
            return None, exc.result()
        try:
            page = read_page(fetched)
        except UnsupportedContent as exc:
            return None, {"error": str(exc), "kind": "unsupported",
                          "final_url": fetched.final_url}
        return (fetched, page, now_utc()), None

    @staticmethod
    def _note(fetched, page) -> str:
        notes = []
        if fetched.truncated:
            notes.append(f"Only the first {len(fetched.body):,} bytes were read.")
        if page.kind == "html" and page.characters < 200:
            notes.append("Almost no readable text: the page may build its content "
                         "with JavaScript, which this reader does not run.")
        if page.unreadable_pages:
            notes.append("Some pages have no text layer (scanned) and could not be read.")
        return " ".join(notes)

    @staticmethod
    def _quote(text: str, match) -> str:
        start = max(0, match.start() - QUOTE_CONTEXT)
        end = min(len(text), match.end() + QUOTE_CONTEXT)
        # Whole words at the edges, so a quote never starts mid-word.
        if start > 0:
            space = text.find(" ", start)
            start = space + 1 if 0 <= space < match.start() else start
        if end < len(text):
            space = text.rfind(" ", match.end(), end)
            end = space if space > match.end() else end
        quote = " ".join(text[start:end].split())
        return ("…" if start > 0 else "") + quote + ("…" if end < len(text) else "")

    @staticmethod
    def _too_large(url, size, exact):
        limit = MAX_SAVE_BYTES
        amount = f"{size:,} bytes" if exact else f"more than {size:,} bytes"
        return {"error": f"The page at {url} is {amount}, and a saved file can be at "
                         f"most {limit:,} bytes. It was not saved; pages.read can "
                         f"still read it in parts.",
                "kind": "too_large", "size": size, "limit": limit}, "error"

    @staticmethod
    def _filename(wanted: str, title: str, url: str, suffix: str) -> str:
        base = wanted or title
        if not base:
            path = urllib.parse.urlsplit(url).path.rstrip("/")
            base = path.rsplit("/", 1)[-1] or urllib.parse.urlsplit(url).hostname or "page"
        name = re.sub(r"[^\w.\- ]", "_", base).strip(" ._")[:100] or "page"
        stem = name[: -len(suffix)] if name.lower().endswith(suffix) else name
        for other in (".html", ".htm", ".pdf", ".md", ".txt"):
            if stem.lower().endswith(other) and other != suffix:
                stem = stem[: -len(other)]
        return stem + suffix
