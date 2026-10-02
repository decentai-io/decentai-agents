# Feeds (`feeds`)

A book fair's news page, a software vendor's release notes, a status
page. Subscribe once with the site's address and every morning the
assistant hears what was published since yesterday — each item once,
and nothing on a quiet day.

Follows RSS and Atom feeds: subscribes to a feed URL or to the feed a
web page advertises, reads a feed without subscribing, and hands on the
items published since the last check. Each subscription is a record
holding its cursor.

No credential.

## Functions

| Function | Level | What it does |
|---|---|---|
| `feeds.add` | 1 | Subscribes to a feed URL, or discovers the feed from a web page's `<link rel="alternate" type="application/rss+xml">` (or `atom+xml`, `rdf+xml`). Starts from now: what the feed already holds is not news. Subscribing to the same feed again returns the existing subscription |
| `feeds.list` | 0 | The subscriptions (25, with the total): how far each was read, consecutive failures |
| `feeds.remove` | 1 | Deletes a subscription |
| `feeds.read` | 0 | The latest items of a feed or a page's feed without subscribing: title, link, published (UTC), a plain-text summary clipped to 300 characters, author |
| `feeds.new_items` | 1, schedulable | New items across subscriptions (or one), oldest first, each naming its feed; moves each cursor through what it returned; `more` when a later check continues. Failures go in `failed` |

Records: `feed`.

## Formats

RSS 2.0, RSS 1.0 (RDF) and Atom 1.0, read with the standard library's
XML parser. Namespaces are ignored, so Dublin Core `dc:date` and
`dc:creator` are read in any of them. Dates in RFC 822 (`Wed, 09 Sep
2026 10:00:00 +0400`) and ISO 8601 (`2026-09-10T08:30:00+02:00`,
`2026-09-01`) are converted to UTC; a date that cannot be read leaves
the item undated rather than guessed. An item's id is its `guid` or
Atom `id`, else its link, else a digest of its title and summary.

A feed that declares XML entities is refused unparsed.

## The cursor, and exactly once

A subscription keeps:

- `cursor_time` — the newest published (or updated) time handed on;
- `cursor_ids` — the ids at exactly that time, so a second item in the
  same second is neither lost nor shown twice;
- `seen_ids` — the latest ids seen (up to 50, within one record field).

An item is new when its id is not in `seen_ids` and it is either later
than the cursor, or at the cursor's time with an id not yet handed on.
**An item without a date counts as new only if its id is not in the
seen list**, and only among the first 50 items of its feed, so a long
undated feed cannot flood the first check.

`new_items` gathers what is new in every feed, sorts it oldest first,
returns at most `max_results` (25 at most) and moves each feed's cursor
only through the items it returned; the rest are next time's, and
`more` says so.

Schedule it with `wake_field: items`. A feed that cannot be fetched or
parsed is listed in `failed` and counted on its record (`failures`,
`last_error`); it is not news and never wakes the assistant alone. A
run checks at most 25 feeds, least recently checked first, and stops
starting new fetches after four minutes.

## Safety: only public addresses

Every fetch — the page, the discovered feed, and each redirect hop —
refuses loopback, private, link-local, multicast, reserved and
unspecified addresses (IPv4 and IPv6), checks the connected address as
well as the resolved one, follows at most 5 redirects, reads at most
5 MB with 10 s connect / 20 s read timeouts, ignores environment
proxies, and names itself `DecentAI-Feeds/0.1`. A page that advertises a
feed on a private address is refused like any other private address.
See `web_reader/README.md` for the details; this agent carries the same
`tools/fetch.py`.

**Test-only:** `DECENTAI_WEB_ALLOW_LOOPBACK=1` permits loopback and
nothing else, for the tests' loopback server. Never set it in a
deployment.

## Limits, and what is not verified

- A feed whose items change their `guid` on every fetch reports them
  again; a feed that re-dates an old item to a later time without a
  stable id may too.
- Seen ids are bounded (50). An undated item that drops out of that
  list while still near the top of its feed could be reported again;
  dated items are protected by the cursor.
- Only the first feed a page advertises is used; give the feed URL to
  choose another.
- Summaries are plain text clipped to 300 characters; follow the link
  (Web Reader) for the rest.
- Tested against a loopback server with hand-written RSS, Atom and RDF
  documents; real feeds have not been exercised.
