# Web Watch (`web_watch`)

A desk lamp is on the wish list at USD 129. Ask to be told when the
price changes, and the assistant hears about it the morning it drops —
once, with the old price and the new one — and not on the mornings it
did not.

Watches public web pages for the change that matters and, on a
schedule, wakes the assistant only when there is one. Each watch is a
record holding its last reading.

No credential.

## Kinds of watch

| Kind | Changes when | Value |
|---|---|---|
| `page` | the readable text changes (whitespace aside) | an excerpt of the start; a short line diff on change |
| `contains` | a phrase appears or disappears (case and line breaks ignored) | `present` / `absent`, with a quote around it |
| `pattern` | the first match of a regular expression changes, appears or disappears | its first group, or the whole match |

Patterns and phrases match the page's **readable text** — what
`web_reader`'s `pages.read` returns — not its markup.

**Pages with rotating content change on every check.** A date in the
header, a visitor counter, a rotating advert or a "related articles"
box makes a `page` watch report a change each time; the text is only
normalised by collapsing whitespace. Use `contains` or `pattern` for
those pages: they ignore everything but what they watch.

## Functions

| Function | Level | What it does |
|---|---|---|
| `watches.add` | 1 | Validates the phrase or regular expression (an invalid one is refused with the reason), takes the first reading at once and stores it: text hash, value or presence, excerpt, snapshot, time |
| `watches.list` | 0 | The watches, least recently checked first (25, with the total): value, last change, consecutive failures |
| `watches.remove` | 1 | Deletes a watch |
| `watches.check_now` | 0 | Fetches one watch now and shows current against stored — changed, before, after, diff — **storing nothing** |
| `watches.changed` | 1, schedulable | Fetches every watch (or one), returns only those that changed in `changed` with before and after, stores the new readings. Failures go in `failed` |

Records: `watch`.

## On a schedule

Schedule `watches.changed` with `wake_field: changed`. A run where
nothing changed returns an empty `changed` and costs no model call.

A page that cannot be fetched — an error status, a timeout, a refused
address, content that is not text — **is not a change**. The reading
stays as it was, the record's `failures` counts consecutive failures
and `last_error` says why, and the watch is listed in `failed`, which is
not the wake field: failures alone never wake the assistant. The next
successful check resets the count and compares with the reading from
before the failures.

A run checks at most 25 pages, least recently checked first, and
stops starting new fetches after four minutes; `more` says a later run
continues.

## Safety: only public addresses

Every fetch, including each redirect hop, refuses loopback, private,
link-local, multicast, reserved and unspecified addresses (IPv4 and
IPv6), checks the connected address as well as the resolved one, follows
at most 5 redirects, reads at most 5 MB with 10 s connect / 20 s read
timeouts, ignores environment proxies, and names itself
`DecentAI-WebWatch/0.1`. See `web_reader/README.md` for the details —
this agent carries the same `tools/fetch.py`.

**Test-only:** `DECENTAI_WEB_ALLOW_LOOPBACK=1` permits loopback and
nothing else, for the tests' loopback server. Never set it in a
deployment.

## Limits, and what is not verified

- The diff is computed from a snapshot of the first 8,000 characters of
  text (a record field holds 8,192). A change further down is still
  detected by the hash; its diff is empty and `note` says so.
- The diff lists at most 40 lines and 4,000 characters.
- A regular expression runs over at most the first million characters.
  One that backtracks catastrophically is stopped by the function's
  timeout, not by the agent.
- No JavaScript runs: a value filled in by the browser is not seen.
- Tested against a loopback server whose pages the tests change between
  checks; real sites have not been exercised.
