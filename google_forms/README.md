# Google Forms (`google_forms`)

"Any new responses to the customer feedback form?" — the responses
submitted since the last look, each keyed by question title, and a
schedule that wakes the assistant only when someone answered. Ask what
a form asks and every question comes back with its kind, its options
and whether it is required.

It reads Google Forms over the Forms API (v1) and changes nothing in
Google: there is no function here that edits a form or a response, and
its Forms scopes are read-only. It rides the same Google connection as
the other Google agents: connect once, grant the one saved account to
each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, and which address it is |
| `forms.find` | 0 | Forms by name, most recently changed first, with links |
| `forms.get` | 0 | Title, description, the link people answer at, the linked response sheet, and every question |
| `responses.list` | 0 | Responses newest first, keyed by question title, paged; optionally only since a time |
| `responses.watch` | 1 | Starts watching a form for new responses, from now |
| `responses.new` | 1 | The responses submitted since the last check; schedulable |

Records: `watch` — a responses watch: the form, the submit time of the
newest response handed on (Google's clock, UTC), and the ids that sit
exactly on that time.

## The watch

1. `responses.watch` takes the newest response's `lastSubmittedTime` as
   the cursor (or the current time, for a form with no responses) and
   remembers the ids on that time. Responses already in are not news.
2. A schedule runs `responses.new` with `wake_field: responses`. Each
   check asks Google only for responses with
   `filter=timestamp >= <cursor>`, sorts them oldest first, leaves out
   the ids already handed on at the cursor's exact time, hands the rest
   on naming their watch, and moves the cursor. Two responses in the
   same instant are neither lost nor shown twice. A quiet check returns
   an empty `responses` and costs no model call.
3. At most `max_results` per watch per check; `more: true` means a later
   check continues.

A response edited after it was handed on has a later submit time and
is handed on again, with its new answers. A form that was deleted, or
that the account can no longer open, closes its watch and is listed in
`closed` with a sentence.

When a form is linked to a response spreadsheet, `forms.get` returns its
`linked_sheet_id`; the Google Sheets agent can read or watch that sheet
instead.

## How a response reads

- Keyed by the question's title; a grid row is "Grid title — Row". A
  second question with the same title gets "(2)" after it.
- Choices are joined with ", "; an answer longer than 500 characters is
  clipped with "…".
- An uploaded file is named ("file: receipt-4471.pdf") and never
  fetched.
- An answer to a question since removed from the form keeps its id as
  its name.
- `email` is filled only when the form collects addresses.

## Setup

Once per organization, by an administrator, in the Google Cloud console:

1. Enable the **Google Forms API** and the **Google Drive API** (Drive
   is how forms are found and which address is connected).
2. OAuth consent screen: add the scopes every Google agent in this
   catalog requests — `openid`, `userinfo.email`, `gmail.modify`,
   `calendar`, `drive`, `spreadsheets`, `documents`,
   `forms.body.readonly`, `forms.responses.readonly`, `tasks` — and
   publish it.
3. Paste the OAuth client into DecentAI under **Settings → Connected
   apps** as provider `google`.

Then, per Google account: **Agents → Google Forms → Credentials →
Connect account**, and grant the saved account to the other Google
agents. An account connected before the scope list grew must be
reconnected once. The `api_base_url` field exists so the tests can
point the agent at a loopback stub; leave it empty.

## Limits, and what is not verified

- Live Google behaviour is **not yet verified**: the tests run the agent
  in a real worker against a loopback stub of the Forms and Drive subset
  it uses. The first real connection is the first live run.
- Not checked against the live API: the exact `filter` syntax
  (`timestamp >= 2026-09-10T08:00:00Z`, unquoted) and that it compares
  against `lastSubmittedTime` rather than `createTime`; the order
  responses come back in (the agent sorts them itself either way); the
  largest `pageSize` (5000 is sent).
- Because Google promises no order, `responses.list` reads every page
  before sorting — up to 20,000 responses, after which `cut_short` is
  true. A watch check reads only what the filter returns.
- A form with no responses is watched from this process's clock, not
  Google's; a response submitted within a clock skew of the watch
  starting could be missed.
- Quiz grades, and images and videos in a form, are not shown.
