"""Subscriptions to RSS and Atom feeds, and what is new in them.

A subscription remembers how far its feed has been read: the newest
item's time (UTC), the ids that sit exactly on that time — so a second
item in the same second is neither lost nor shown twice — and the ids
of the latest items seen, for feeds whose items carry no date at all.

new_items() hands on what appeared since and moves each cursor only
through what it handed on; its ``items`` list is what a schedule wakes
the assistant on. A feed that could not be fetched is counted on its
record and listed in ``failed``, which wakes no one by itself.
"""

import time
from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .feed_parse import (FEED_TYPES, FeedProblem, advertised_feeds, looks_like_feed,
                         parse_feed)
from .fetch import FetchError, SafeFetcher

USER_AGENT = "DecentAI-Feeds/0.1 (reads a feed a DecentAI user subscribed to)"
ACCEPT = ", ".join(FEED_TYPES) + ", application/xml;q=0.9, text/html;q=0.8, */*;q=0.5"

# The ids remembered per feed: as many of the latest as fit in a record's
# text field (8,192 characters), and no more than SEEN_LIMIT. An undated
# item counts as new only within the first SEEN_LIMIT items of its feed,
# so a long undated feed cannot flood the first check.
SEEN_LIMIT = 50
SEEN_CHARS = 8000
LIST_SHOWN = 25
PER_RUN = 25
RUN_BUDGET_SECONDS = 240


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FeedsTool(ToolBase):
    id = "feeds"

    async def add(self, call):
        url = str(call.inputs["url"]).strip()
        await call.progress(f"Looking for a feed at {url}")
        try:
            feed, page_url = self._resolve(url)
        except FetchError as exc:
            return exc.result(), "error"
        except FeedProblem as exc:
            return {"error": str(exc), "kind": "not_a_feed"}, "error"
        existing = await call.resources.list_data("feed", {"feed_url": feed.url})
        if existing:
            keys = existing[0].get("keys") or {}
            return {"feed_ref": existing[0]["resource_ref"], "title": str(keys.get("title") or ""),
                    "feed_url": feed.url, "discovered_from": page_url, "format": feed.format,
                    "items": len(feed.items), "newest": self._cursor(feed.items)[0],
                    "already": True}, "success"
        cursor_time, cursor_ids = self._cursor(feed.items)
        seen = self._seen_list([item.id for item in feed.items[:SEEN_LIMIT]], [])
        at = now_utc()
        record = await call.resources.create_data("feed", {
            "feed_url": feed.url, "page_url": page_url, "title": feed.title,
            "format": feed.format, "cursor_time": cursor_time,
            "cursor_ids": "\n".join(sorted(cursor_ids)), "seen_ids": "\n".join(seen),
            "added_at": at, "checked_at": at, "failures": 0, "last_error": "",
            "note": str(call.inputs.get("note") or ""),
        })
        return {"feed_ref": record["resource_ref"], "title": feed.title,
                "feed_url": feed.url, "discovered_from": page_url, "format": feed.format,
                "items": len(feed.items), "newest": cursor_time, "already": False}, "success"

    async def list(self, call):
        records = await call.resources.list_data("feed", {})
        records.sort(key=lambda r: str((r.get("keys") or {}).get("title") or "").lower())
        rows = []
        for record in records[:LIST_SHOWN]:
            keys = record.get("keys") or {}
            rows.append({"feed_ref": str(record.get("resource_ref") or ""),
                         "title": str(keys.get("title") or ""),
                         "feed_url": str(keys.get("feed_url") or ""),
                         "page_url": str(keys.get("page_url") or ""),
                         "format": str(keys.get("format") or ""),
                         "read_up_to": str(keys.get("cursor_time") or ""),
                         "checked_at": str(keys.get("checked_at") or ""),
                         "failures": int(keys.get("failures") or 0),
                         "last_error": str(keys.get("last_error") or "")})
        return {"feeds": rows, "total": len(records)}, "success"

    async def remove(self, call):
        ref = str(call.inputs["feed_ref"])
        try:
            record = await call.resources.read_data("feed", ref)
        except Exception:
            return {"error": f"There is no subscription {ref}.", "kind": "not_found"}, "error"
        deleted = await call.resources.delete_data("feed", ref)
        keys = record.get("keys") or {}
        return {"removed": bool(deleted), "feed_ref": ref,
                "title": str(keys.get("title") or ""),
                "feed_url": str(keys.get("feed_url") or "")}, "success"

    async def read(self, call):
        url = str(call.inputs["url"]).strip()
        most = int(call.inputs.get("max_results") or 10)
        await call.progress(f"Reading the feed at {url}")
        try:
            feed, page_url = self._resolve(url)
        except FetchError as exc:
            return exc.result(), "error"
        except FeedProblem as exc:
            return {"error": str(exc), "kind": "not_a_feed"}, "error"
        return {"feed_url": feed.url, "discovered_from": page_url, "title": feed.title,
                "format": feed.format, "items": [i.row() for i in feed.items[:most]],
                "total": len(feed.items)}, "success"

    async def new_items(self, call):
        wanted = str(call.inputs.get("feed_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            try:
                records = [await call.resources.read_data("feed", wanted)]
            except Exception:
                return {"error": f"There is no subscription {wanted}.",
                        "kind": "not_found"}, "error"
        else:
            records = await call.resources.list_data("feed", {})
        records.sort(key=lambda r: str((r.get("keys") or {}).get("checked_at") or ""))
        more = len(records) > PER_RUN
        started = time.monotonic()

        read, failed, candidates = [], [], []
        for order, record in enumerate(records[:PER_RUN]):
            if time.monotonic() - started > RUN_BUDGET_SECONDS:
                more = True
                break
            ref = str(record.get("resource_ref") or "")
            keys = record.get("keys") or {}
            try:
                feed = self._fetch_feed(str(keys.get("feed_url") or ""))
            except (FetchError, FeedProblem) as exc:
                failures = int(keys.get("failures") or 0) + 1
                await call.resources.update_data("feed", ref, {
                    "failures": failures, "last_error": str(exc)[:500],
                    "checked_at": now_utc()})
                failed.append({"feed_ref": ref, "title": str(keys.get("title") or ""),
                               "feed_url": str(keys.get("feed_url") or ""),
                               "error": str(exc)[:500], "failures": failures})
                continue
            read.append((ref, keys, feed))
            for position, item in self._unseen(keys, feed):
                # Oldest first: dated items by time, then undated ones. A
                # feed lists newest first, so where times tie or are
                # missing, the item further down came first.
                sort_key = (0 if item.published else 1, item.published, order, -position)
                candidates.append((sort_key, ref, feed, item))

        candidates.sort(key=lambda c: c[0])
        if len(candidates) > most:
            more = True
        handed = candidates[:most]
        rows = [{"feed_ref": ref, "feed_title": feed.title, "feed_url": feed.url,
                 **item.row()} for _, ref, feed, item in handed]

        for ref, keys, feed in read:
            mine = [item for _, r, _, item in handed if r == ref]
            await call.resources.update_data("feed", ref, self._moved(keys, feed, mine))
        return {"checked": len(read) + len(failed), "items": rows, "failed": failed,
                "more": more}, "success"

    # ------------------------------------------------------------------
    def _resolve(self, url):
        """(feed, the page it was discovered from or '') — a feed URL is
        read as it is; a web page is asked which feed it advertises."""
        fetched = SafeFetcher(USER_AGENT).get(url, accept=ACCEPT)
        if self._is_feed(fetched):
            return self._parsed(fetched), ""
        if "html" not in fetched.content_type and \
                not fetched.body[:512].lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise FeedProblem(f"{fetched.final_url} is {fetched.content_type or 'not'} "
                              f"a feed or a web page.")
        found = advertised_feeds(fetched.text(), fetched.final_url)
        if not found:
            raise FeedProblem(f"{fetched.final_url} is a web page that advertises no RSS "
                              f"or Atom feed. Ask for the feed's own address.")
        return self._fetch_feed(found[0]), fetched.final_url

    def _fetch_feed(self, url):
        fetched = SafeFetcher(USER_AGENT).get(url, accept=ACCEPT)
        if not self._is_feed(fetched):
            raise FeedProblem(f"{fetched.final_url} is not an RSS or Atom feed "
                              f"({fetched.content_type or 'unknown type'}).")
        return self._parsed(fetched)

    @staticmethod
    def _is_feed(fetched) -> bool:
        return fetched.content_type in FEED_TYPES or looks_like_feed(fetched.body)

    @staticmethod
    def _parsed(fetched):
        if fetched.truncated:
            raise FeedProblem(f"The feed at {fetched.final_url} is larger than "
                              f"{len(fetched.body):,} bytes and was not read.")
        return parse_feed(fetched.body, fetched.final_url)

    @staticmethod
    def _cursor(items):
        """(newest time, the ids at exactly that time)."""
        dated = [item.published for item in items if item.published]
        if not dated:
            return "", set()
        newest = max(dated)
        return newest, {item.id for item in items if item.published == newest}

    @staticmethod
    def _unseen(keys, feed):
        seen = {i for i in str(keys.get("seen_ids") or "").split("\n") if i}
        cursor = str(keys.get("cursor_time") or "")
        at_cursor = {i for i in str(keys.get("cursor_ids") or "").split("\n") if i}
        out, ids = [], set()
        for position, item in enumerate(feed.items):
            if item.id in seen or item.id in ids:
                continue
            if item.published:
                if cursor and (item.published < cursor or
                               (item.published == cursor and item.id in at_cursor)):
                    continue
            elif position >= SEEN_LIMIT:
                continue
            ids.add(item.id)
            out.append((position, item))
        return out

    def _moved(self, keys, feed, handed) -> dict:
        """The record's new cursor: forward only through what was handed on."""
        cursor = str(keys.get("cursor_time") or "")
        at_cursor = {i for i in str(keys.get("cursor_ids") or "").split("\n") if i}
        newest, at_newest = self._cursor(handed)
        if newest and newest > cursor:
            cursor, at_cursor = newest, at_newest
        elif newest and newest == cursor:
            at_cursor |= at_newest
        current = [item.id for item in feed.items]
        old_seen = [i for i in str(keys.get("seen_ids") or "").split("\n") if i]
        seen = self._seen_list([item.id for item in reversed(handed)],
                               [i for i in old_seen if i in current] +
                               [i for i in old_seen if i not in current])
        return {"cursor_time": cursor, "cursor_ids": "\n".join(sorted(at_cursor)),
                "seen_ids": "\n".join(seen), "title": feed.title or str(keys.get("title") or ""),
                "checked_at": now_utc(), "failures": 0, "last_error": ""}

    @staticmethod
    def _seen_list(first, then):
        kept, size = [], 0
        for ident in list(first) + list(then):
            if ident in kept:
                continue
            if len(kept) >= SEEN_LIMIT or size + len(ident) + 1 > SEEN_CHARS:
                break
            kept.append(ident)
            size += len(ident) + 1
        return kept
