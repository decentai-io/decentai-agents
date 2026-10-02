"""Watches: a page, and what about it matters.

A watch keeps its last reading as a record — the hash of the page's
readable text, the value it is watching for, a short excerpt, and a
bounded snapshot of the lines for a diff — and changed() compares a
fresh reading with it. Three kinds, because "any change" is the wrong
question for most pages:

- ``page``: any change to the readable text (whitespace aside);
- ``contains``: a phrase appearing or disappearing;
- ``pattern``: the first match of a regular expression — a price, a
  status word — changing, appearing or disappearing.

changed() returns only the watches that changed, in ``changed``: that
is what a schedule wakes the assistant on, so a quiet page costs no
model call. A page that could not be fetched is not a change; it is
counted on the record and reported in ``failed``, which does not wake
anyone by itself.
"""

import difflib
import hashlib
import re
import time
from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .fetch import FetchError, SafeFetcher
from .page_text import UnsupportedContent, phrase_pattern, read_page

USER_AGENT = "DecentAI-WebWatch/0.1 (checks a page a DecentAI user asked to watch)"

# A record's text field holds at most 8,192 characters; the snapshot the
# diff is computed from stays under that.
SNAPSHOT_CHARS = 8000
EXCERPT_CHARS = 300
QUOTE_CONTEXT = 100
DIFF_LINES = 40
DIFF_CHARS = 4000
LIST_SHOWN = 25
PER_RUN = 25
RUN_BUDGET_SECONDS = 240
# A regular expression runs over at most this much text: a pattern that
# backtracks badly is bounded by the input as well as the call's timeout.
SEARCH_CHARS = 1_000_000


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Reading:
    """One look at a page, in the forms a watch compares."""

    def __init__(self, url: str):
        fetched = SafeFetcher(USER_AGENT).get(url)
        page = read_page(fetched)
        self.final_url = fetched.final_url
        self.lines = page.lines()
        self.text = "\n".join(self.lines)[:SEARCH_CHARS]
        flat = " ".join(self.text.split())
        self.hash = hashlib.sha256(flat.encode("utf-8")).hexdigest()
        self.opening = flat[:EXCERPT_CHARS]
        self.snapshot = self._snapshot(self.lines)
        self.at = now_utc()

    @staticmethod
    def _snapshot(lines) -> str:
        kept, size = [], 0
        for line in lines:
            if size + len(line) + 1 > SNAPSHOT_CHARS:
                break
            kept.append(line)
            size += len(line) + 1
        return "\n".join(kept)

    def look_for(self, kind: str, target: str) -> dict:
        """{found, value, excerpt} for this watch's kind."""
        if kind == "page":
            return {"found": True, "value": "", "excerpt": self.opening}
        if kind == "contains":
            match = phrase_pattern(target).search(self.text)
        else:
            match = re.compile(target).search(self.text)
        if match is None:
            return {"found": False, "value": "", "excerpt": ""}
        if kind == "pattern" and match.groups():
            value = match.group(1) or ""
        else:
            value = match.group(0)
        return {"found": True, "value": " ".join(value.split())[:200],
                "excerpt": self._quote(match)}

    def _quote(self, match) -> str:
        start = max(0, match.start() - QUOTE_CONTEXT)
        end = min(len(self.text), match.end() + QUOTE_CONTEXT)
        quote = " ".join(self.text[start:end].split())
        return ("…" if start else "") + quote + ("…" if end < len(self.text) else "")


class WatchesTool(ToolBase):
    id = "watches"

    async def add(self, call):
        url = str(call.inputs["url"]).strip()
        kind = str(call.inputs["kind"])
        target, refusal = self._target(kind, call.inputs)
        if refusal:
            return {"error": refusal, "kind": "invalid"}, "error"
        await call.progress(f"Taking a first reading of {url}")
        try:
            reading = Reading(url)
        except FetchError as exc:
            return exc.result(), "error"
        except UnsupportedContent as exc:
            return {"error": str(exc), "kind": "unsupported"}, "error"
        seen = reading.look_for(kind, target)
        record = await call.resources.create_data("watch", {
            "url": url, "final_url": reading.final_url, "kind": kind,
            "target": target, "label": str(call.inputs.get("label") or ""),
            "value": seen["value"], "found": 1 if seen["found"] else 0,
            "excerpt": seen["excerpt"], "hash": reading.hash,
            "snapshot": reading.snapshot, "checked_at": reading.at,
            "tried_at": reading.at, "changed_at": "", "failures": 0,
            "last_error": "",
        })
        result = {"watch_ref": record["resource_ref"], "url": url,
                  "final_url": reading.final_url, "kind": kind, "target": target,
                  "found": seen["found"], "value": seen["value"],
                  "excerpt": seen["excerpt"], "checked_at": reading.at}
        if kind != "page" and not seen["found"]:
            result["note"] = ("Not on the page now; the watch will report when it "
                              "appears.")
        return result, "success"

    async def list(self, call):
        records = await call.resources.list_data("watch", {})
        records.sort(key=lambda r: str((r.get("keys") or {}).get("tried_at") or ""))
        rows = [self._row(r) for r in records[:LIST_SHOWN]]
        return {"watches": rows, "total": len(records)}, "success"

    async def remove(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            record = await call.resources.read_data("watch", ref)
        except Exception:
            return {"error": f"There is no watch {ref}.", "kind": "not_found"}, "error"
        deleted = await call.resources.delete_data("watch", ref)
        keys = record.get("keys") or {}
        return {"removed": bool(deleted), "watch_ref": ref,
                "url": str(keys.get("url") or "")}, "success"

    async def check_now(self, call):
        ref = str(call.inputs["watch_ref"])
        try:
            record = await call.resources.read_data("watch", ref)
        except Exception:
            return {"error": f"There is no watch {ref}.", "kind": "not_found"}, "error"
        keys = record.get("keys") or {}
        await call.progress(f"Checking {keys.get('url')}")
        try:
            reading = Reading(str(keys.get("url") or ""))
        except FetchError as exc:
            return exc.result(), "error"
        except UnsupportedContent as exc:
            return {"error": str(exc), "kind": "unsupported"}, "error"
        # Shown, never stored: the next changed() still compares with the
        # reading the record holds.
        comparison = self._compare(ref, keys, reading)
        return comparison, "success"

    async def changed(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        if wanted:
            try:
                records = [await call.resources.read_data("watch", wanted)]
            except Exception:
                return {"error": f"There is no watch {wanted}.", "kind": "not_found"}, "error"
        else:
            records = await call.resources.list_data("watch", {})
        # Longest unchecked first, so that past PER_RUN every watch still
        # gets its turn on a later run.
        records.sort(key=lambda r: str((r.get("keys") or {}).get("tried_at") or ""))
        started = time.monotonic()
        changed, failed, checked, more = [], [], 0, len(records) > PER_RUN
        for record in records[:PER_RUN]:
            if time.monotonic() - started > RUN_BUDGET_SECONDS:
                more = True
                break
            checked += 1
            ref = str(record.get("resource_ref") or "")
            keys = record.get("keys") or {}
            try:
                reading = Reading(str(keys.get("url") or ""))
            except (FetchError, UnsupportedContent) as exc:
                failures = int(keys.get("failures") or 0) + 1
                await call.resources.update_data("watch", ref, {
                    "failures": failures, "last_error": str(exc)[:500],
                    "tried_at": now_utc()})
                failed.append({"watch_ref": ref, "label": str(keys.get("label") or ""),
                               "url": str(keys.get("url") or ""), "error": str(exc)[:500],
                               "failures": failures})
                continue
            comparison = self._compare(ref, keys, reading)
            update = {"final_url": reading.final_url, "value": comparison["after_value"],
                      "found": 1 if comparison["found_after"] else 0,
                      "excerpt": comparison["excerpt_after"], "hash": reading.hash,
                      "snapshot": reading.snapshot, "checked_at": reading.at,
                      "tried_at": reading.at, "failures": 0, "last_error": ""}
            if comparison["changed"]:
                update["changed_at"] = reading.at
                changed.append(comparison)
            await call.resources.update_data("watch", ref, update)
        return {"checked": checked, "changed": changed, "failed": failed,
                "more": more}, "success"

    # ------------------------------------------------------------------
    @staticmethod
    def _target(kind, inputs):
        """(target, refusal) for a new watch."""
        if kind == "contains":
            phrase = " ".join(str(inputs.get("phrase") or "").split())
            if not phrase:
                return "", "A contains watch needs the phrase to look for."
            return phrase, ""
        if kind == "pattern":
            pattern = str(inputs.get("pattern") or "")
            if not pattern.strip():
                return "", "A pattern watch needs a regular expression."
            try:
                re.compile(pattern)
            except re.error as exc:
                return "", (f"The pattern is not a valid regular expression: {exc.msg}"
                            f" (at position {exc.pos}).")
            return pattern, ""
        return "", ""

    @staticmethod
    def _shown(kind, found, value, excerpt) -> str:
        if kind == "page":
            return excerpt
        if kind == "contains":
            return "present" if found else "absent"
        return value if found else "(no match)"

    def _compare(self, ref, keys, reading) -> dict:
        kind = str(keys.get("kind") or "page")
        target = str(keys.get("target") or "")
        now = reading.look_for(kind, target)
        was_found = bool(int(keys.get("found") or 0))
        was_value = str(keys.get("value") or "")
        was_excerpt = str(keys.get("excerpt") or "")
        if kind == "page":
            changed = reading.hash != str(keys.get("hash") or "")
        elif kind == "contains":
            changed = now["found"] != was_found
        else:
            changed = now["found"] != was_found or now["value"] != was_value
        entry = {
            "watch_ref": ref, "label": str(keys.get("label") or ""),
            "url": str(keys.get("url") or ""), "final_url": reading.final_url,
            "kind": kind, "target": target, "changed": changed,
            "before": self._shown(kind, was_found, was_value, was_excerpt),
            "after": self._shown(kind, now["found"], now["value"], now["excerpt"]),
            "found_before": was_found, "found_after": now["found"],
            "before_value": was_value, "after_value": now["value"],
            "excerpt_before": was_excerpt, "excerpt_after": now["excerpt"],
            "before_checked_at": str(keys.get("checked_at") or ""),
            "checked_at": reading.at,
        }
        if kind == "page" and changed:
            diff, truncated = self._diff(str(keys.get("snapshot") or ""), reading.snapshot)
            entry["diff"], entry["diff_truncated"] = diff, truncated
            if not diff:
                entry["note"] = (f"The text changed beyond the first {SNAPSHOT_CHARS:,} "
                                 f"characters kept for comparison; read the page to see it.")
        return entry

    @staticmethod
    def _diff(before: str, after: str):
        """Removed lines as '- …' and added ones as '+ …', capped."""
        old = before.split("\n") if before else []
        new = after.split("\n") if after else []
        lines = []
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            lines.extend(f"- {line}" for line in old[i1:i2])
            lines.extend(f"+ {line}" for line in new[j1:j2])
        kept, size = [], 0
        for line in lines:
            line = line if len(line) <= 300 else line[:300] + "…"
            if len(kept) >= DIFF_LINES or size + len(line) > DIFF_CHARS:
                return kept, True
            kept.append(line)
            size += len(line)
        return kept, False

    @staticmethod
    def _row(record) -> dict:
        keys = record.get("keys") or {}
        return {"watch_ref": str(record.get("resource_ref") or ""),
                "label": str(keys.get("label") or ""), "url": str(keys.get("url") or ""),
                "kind": str(keys.get("kind") or ""), "target": str(keys.get("target") or ""),
                "found": bool(int(keys.get("found") or 0)),
                "value": str(keys.get("value") or ""),
                "excerpt": str(keys.get("excerpt") or ""),
                "checked_at": str(keys.get("checked_at") or ""),
                "changed_at": str(keys.get("changed_at") or ""),
                "failures": int(keys.get("failures") or 0),
                "last_error": str(keys.get("last_error") or "")}
