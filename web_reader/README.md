# Web Reader (`web_reader`)

Someone pastes a link to a supplier's terms page and asks whether it
says anything about refunds. The answer comes back with the section it
was read from and the sentence itself — or a plain "the page does not
mention refunds".

Reads a public web page, an online PDF, or a text or JSON URL with the
same rules as Documents: the text in numbered parts with totals, the
passages that contain a phrase with their section or page, and a copy
saved as a platform file that Documents (or any agent) can read by
`file_ref`.

No credential, no records. Every call fetches afresh.

## Functions

| Function | Level | What it does |
|---|---|---|
| `pages.read` | 0 | Final URL after redirects, title, content type and the readable text in numbered parts — sections of a web page (split at headings, a long section split further), pages of a PDF, parts of plain text — from `from`, up to `max_chars`, with `total_parts`, `total_chars` and `next`. Links found, as text and absolute URL (first 25, with the total). PDF pages with no text layer are listed |
| `pages.find` | 0 | One phrase or up to ten, matched regardless of case and line breaks. Each match with its part number, heading and a short quote; `absent` lists the phrases the page does not contain |
| `pages.save` | 2 | A PDF as its original bytes; a web page as Markdown of its readable text; other text as `.txt` — each headed with `Source:` and `Fetched:`. At most 25 MB; a larger page is refused with its size |

Files: `page` (PDF, Markdown, plain text; 25 MB, what the platform
can carry).

## How a page is read

| Kind | Read as |
|---|---|
| HTML | Standard-library parser. Dropped: `script`, `style`, `noscript`, `template`, `svg`, `nav`, `footer`, `aside`, `iframe`, `button`, `select`, anything `hidden` or `aria-hidden`, and elements with role navigation, banner, contentinfo, complementary or search. Headings become `# …` lines and start a section; list items keep a dash; table cells are separated by `|` |
| PDF | pypdf, page by page; the document title from its metadata |
| Text, JSON, XML | Passed through, split into parts of at most 4,000 characters at line boundaries |
| Anything else (images, archives) | Refused as unsupported, naming the content type |

The charset is the one the server names, else the one the page
declares, else UTF-8.

## Safety: only public addresses

An assistant that fetches whatever URL it is given can be aimed at the
network it runs in. Every fetch (`tools/fetch.py`):

- accepts only `http` and `https`, and no user name or password in the URL;
- resolves the host and refuses it if **any** address is loopback,
  private, link-local (including `169.254.169.254`), multicast,
  reserved, unspecified or otherwise not globally routable — IPv4, IPv6
  and IPv4-mapped IPv6 — and says which;
- checks the address the socket actually connected to as well, so a
  name that resolves differently the second time (DNS rebinding) is
  still refused;
- follows at most 5 redirects itself, checking every hop;
- reads at most 5 MB (the rest is not read, and `body_truncated` says
  so), with a 10 s connect timeout, a 20 s read timeout and 45 s for
  the whole body;
- ignores proxy settings in the environment (a proxy would be the
  connected address, not the target);
- sends `User-Agent: DecentAI-WebReader/0.1 (…)`.

**Test-only:** `DECENTAI_WEB_ALLOW_LOOPBACK=1` permits loopback
(127.0.0.0/8, `::1`) and nothing else — never a private range — so the
tests can serve pages from a loopback stub. A deployment must never set
it; the tests prove a private address is refused even with it set, and
loopback without it.

## Limits, and what is not verified

- No JavaScript runs. A page that builds its text in the browser reads
  as nearly empty; `note` says so when a web page yields under 200
  characters.
- Boilerplate removal is by element and role, not by layout: a site
  whose menus are plain `div`s keeps them.
- A scanned PDF page has no text layer and is listed in
  `unreadable_pages`. A PDF over 5 MB is not read at all.
- Pages behind a login, a cookie wall or bot protection read as what
  that wall says.
- The address rules rely on the resolver the worker uses; a site
  reachable only through a proxy cannot be fetched.
- Tested against a loopback server with hand-written HTML, PDFs and
  redirects. Real sites on the internet, TLS and IPv6 connectivity have
  not been exercised by the tests.
