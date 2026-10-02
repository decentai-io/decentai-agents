# Notion (`notion`)

"Which office move tasks are still in progress, and what does the
launch plan still have open?" — found by title, read as text, and
looked through with a simple filter. Ask to add a task, tick a status,
append a checklist or leave a comment, and each change waits for your
go-ahead, is checked against the database's own schema first, and is
kept on record.

One Notion workspace over the Notion API, as far as the person shared
it with the integration while signing in. The agent sees only those
pages (and what sits under them); a page it cannot open is a page to
share, and its errors say so.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, the workspace's name and the integration's |
| `search.find` | 0 | Pages and databases by title, newest edited first; narrowed to pages or databases |
| `pages.read` | 0 | A page's properties as text, and its body as one line per block, a window at a time |
| `databases.get` | 0 | A database's schema: property names, types, and select / multi-select / status options |
| `databases.query` | 0 | Rows with properties as text; one simple filter and a sort; paged |
| `pages.create` | 3 | A row in a database, validated against its schema, or a page under a page; optional paragraphs |
| `pages.update` | 3 | Changes a row's properties (or a page's title); records what each was before |
| `blocks.append` | 3 | Adds paragraphs, to-dos and bullets to the end of a page |
| `comments.list` | 0 | A page's comments |
| `comments.add` | 3 | A page-level comment, shown as written by the integration |
| `watch.database` | 1 | Records a watch on a database, from now in Notion's clock |
| `watch.changes` | 1, schedulable | Rows created or edited since the last check, each marked `created` or `edited`; moves the watch |

Records: `write` — every page, row, property change, block append and
comment this agent made, with the previous values of an update; and
`watch` — the databases followed and how far each was read.

## Rules it keeps

- **Nothing in the workspace changes without a yes.** Creating,
  updating, appending and commenting are all level 3: the workspace is
  one other people share.
- **The schema decides what a row may hold.** Before a row is written
  the agent reads the database and refuses a property that does not
  exist or an option that is not defined, naming the ones that are.
  Notion itself would silently add a new select option; this agent
  does not. Computed properties (formulas, rollups, created time,
  unique ids) are refused, and so are people and files, which need ids
  a conversation does not have.
- **Nothing is invented.** Page, database and comment ids come from a
  search, a query or a page read.
- **Results stay small.** At most 25 rows or results per call (20 by
  default); a page is read at most 100 blocks at a time with `from`,
  `next_from` and a `total`; one block's text is clipped at 2,000
  characters and a property's at 500, and a clipped text ends in "…".
- **One level down.** The children of a toggle or a list item are read
  with it (`depth: 1`); anything deeper is named "has nested content",
  and child pages and databases are named with their ids, not opened.
- **Update keeps the before.** `pages.update` returns and records each
  changed property's previous value, so a change can be undone by hand.

## Following a database

"Tell me when anything in Office move tasks changes":

1. `watch.database {database_id}` → `watch_ref`.
2. The assistant schedules `watch.changes` with `wake_field: rows` and,
   optionally, `inputs: {watch_ref}`.
3. The clock runs it with no model. If nothing changed, `rows` is empty
   and nobody is woken. If something did, each row says what it is,
   whether it was `created` or `edited`, and which watch it belongs to;
   `more: true` means a later check continues where this one stopped.

**Minute granularity, honestly.** Notion keeps `last_edited_time` to
the minute. The watch's cursor is the newest minute it handed on, plus
the rows it saw on that minute and the time it saw each, so two rows
changed in the same minute are both reported, and each exactly once. What
it cannot see is a *second* edit to a row inside the minute it was
already reported in: the row still carries the same time, and the
change surfaces with that row's next edit in a later minute. A row is
`created` when it came into being after the cursor (or on the cursor's
minute without having been seen there); otherwise `edited`.

## Errors

| kind | when |
|---|---|
| `auth` | the token was revoked or has expired — reconnect the workspace from its Credentials page |
| `not_shared` | Notion answered 403 `restricted_resource` — share the page with the integration (the page's ••• menu → Connections) or reconnect and select it |
| `not_found` | Notion answered 404 `object_not_found` — no such id, *or* not shared; Notion does not tell these apart, so the same hint is given |
| `rate_limited` | Notion answered 429; `retry_after_seconds` carries its Retry-After |
| `http` | anything else Notion refused, including a 409 conflict |
| `unknown` | a write got no answer: the outcome is unknown, and it is never retried |
| `invalid` | the agent refused before sending: an unknown property or option, a bad date, a missing title |

Reads are retried once on a dropped connection; writes never are.

## The API version, pinned

Every request carries `Notion-Version: 2022-06-28`. Newer versions
reshape a database into one or more "data sources", with different
endpoints for querying and creating rows. This agent pins the stable
version on purpose; moving it is a deliberate new version of the agent,
not a silent upgrade.

## Setup

Once per organization, by an administrator, at
[notion.so/my-integrations](https://www.notion.so/my-integrations):

1. Create a new integration of type **Public**.
2. Capabilities: **Read content**, **Update content**, **Insert
   content**, **Read comments**, **Insert comments**, and user
   capabilities **Read user information including email addresses**
   (the platform names the connection after that address).
3. Add the redirect URI DecentAI shows under **Settings → Connected
   apps**.
4. Copy the OAuth client id and client secret into DecentAI under
   **Settings → Connected apps** as provider `notion`.

Then, per workspace, anyone: **Agents → Notion → Credentials → Connect
account**. Notion's sign-in asks which pages to share with the
integration; the agent sees only the pages chosen there and the pages
beneath them. To give it more later, share a page with the integration
from Notion's ••• menu → Connections, or connect again.

Notion grants what the integration's capabilities say rather than
OAuth scopes, so the manifest's `scopes` entry is a placeholder the
contract requires and Notion ignores. The token request authenticates
the client with HTTP Basic and sends JSON, and the account's email
comes back in the token response itself (`owner.user.person.email`).

## Limits

- Titles only: Notion's search does not look inside page text.
- Comments are page-level; comment authors are user ids, not names.
- A page's total is counted up to 1,000 top-level blocks
  (`total_capped` says when it stopped); a block's children up to 100.
- A filter is one condition; compound filters and relation, people or
  rollup filters are left to Notion itself.
- Live Notion behaviour is verified against a loopback stub of the API
  subset it uses; the first real connection is the first live run.
