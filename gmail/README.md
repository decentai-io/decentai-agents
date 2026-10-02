# Gmail (`gmail`)

Dana at Harbourline asks for a quotation for twenty desks. Ask the
assistant to find her email, draft the reply, and remind you on Thursday
if she has not answered. The draft appears in Gmail before it goes
anywhere; the send needs your go-ahead; and Thursday's reminder fires
only if the thread is still quiet.

Finds and reads Gmail conversations, prepares replies and new messages
as Gmail drafts, sends a draft only on explicit authorization, saves
attachments as files for other agents, watches a thread for a reply
so a schedule can remind the user only while the reply is still
missing, and watches the inbox so a schedule can wake the assistant
only when mail has arrived.

One Google account per credential, connected by signing in: the
platform runs the consent, keeps the refresh token and refreshes it;
the agent receives a short-lived access token and the address. A
person may connect more than one: `account.list` names them, every
function that reads or writes mail takes an optional `account` (the
address), and a draft, a watch and an inbox watch remember the account
they were made on. With none named, the person's default answers, or
the only account; a shared mailbox is a connected account granted to
the agent org-wide.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection and which address it is |
| `account.list` | 0 | The connected accounts by address, and which is the default |
| `search.find` | 0 | Gmail query syntax → one row per message, newest first, with `thread_id` and a link; `page_token` continues |
| `search.thread` | 0 | One conversation whole: senders, dates, body text (cut at `max_body_chars`), attachment list with ids |
| `search.save_attachment` | 2 | One attachment saved as a file (`file_ref`) for Documents or Spreadsheets to read |
| `draft.reply` | 1 | A reply prepared as a Gmail draft in the thread, to the last sender unless `to` is given. Nothing is sent |
| `draft.compose` | 1 | A new message prepared as a Gmail draft. Nothing is sent |
| `draft.send` | 3 | Sends a prepared draft. Succeeds only when Gmail returns the sent message's id |
| `draft.discard` | 1 | Deletes the Gmail draft and marks the record discarded |
| `watch.await_reply` | 1 | Records that the user waits for a reply on a thread, from now |
| `watch.check` | 1, schedulable | Checks waiting watches; `unanswered` lists threads still without a reply from anyone else |
| `watch.inbox` | 1 | Starts watching the inbox from now (or from `since`); `only_from` / `skip_from` name the senders that count, as addresses or `@domain`s |
| `watch.new_mail` | 1, schedulable | What arrived since the last check, oldest first, one row per message naming its watch; moves the watch forward; `more` says a later check continues |

Records: `draft` (what was prepared and what became of it), `watch`
(threads awaited) and `inbox` (how far the inbox has been read). Files:
`attachment` (what was saved out of mail).

## Watching the inbox

The assistant records one inbox watch and schedules `watch.new_mail`
with `wake_field: messages` every few minutes. A check that finds
nothing costs no model call; a check that finds mail wakes the
assistant with the rows, and the standing instruction in that chat
says what to do with each. The watch is a timestamp cursor in Gmail's
own clock — the time of the newest message handed on, plus the ids
that sit exactly on that second — so a second mail in the same second
is neither lost nor shown twice. Gmail lists newest first and cannot be
asked for oldest first, so a check walks the ids that arrived since
the cursor and reads the oldest of them; a burst larger than
`max_results` is handed on across checks, never skipped. Mail from the
account itself never counts; `only_from` and `skip_from` keep a
newsletter from costing a wake. The cursor moves when the rows are
handed on, so a wake the assistant failed to act on is not offered
again.

## The reminder pattern

"Remind me tomorrow if they haven't replied":

1. `watch.await_reply {thread_id}` → `watch_ref`.
2. The assistant schedules `watch.check` for tomorrow with
   `wake_field: unanswered` and, optionally, `inputs: {watch_ref}`.
3. The clock runs it with no model. If the reply came, `unanswered` is
   empty and nobody is woken. If not, the assistant wakes with the
   thread and the note, and decides what to do.

## Send outcomes

`draft.send` has three outcomes and words them differently:

- **sent** — Gmail returned the sent message's id; recorded on the draft.
- **error** — Gmail refused; the draft stays `prepared` and may be
  sent again once the cause is fixed.
- **unknown** — the request was made and no answer came back. The draft
  is marked `unknown` and this function will not send it again; the
  user should look in the Sent folder. Reads are retried once on a
  dropped connection; writes never are.

A grant the provider no longer accepts is reported as `kind: auth`
with the instruction to reconnect; the credential shows *needs
reconnect* on the Credentials page. Every message and thread id, and
every link, is Gmail's own.

## Setup

Once per organization, by an administrator, in the Google Cloud console:

1. Enable the APIs the Google agents call under APIs & Services →
   Library: **Gmail**, **Google Calendar**, **Google Drive**, **Google
   Sheets**, **Google Docs**, **Google Forms** and **Google Tasks**.
2. OAuth consent screen: type External, the scopes every Google agent
   in the catalog shares — `openid`, `userinfo.email`, `gmail.modify`,
   `calendar`, `drive`, `spreadsheets`, `documents`,
   `forms.body.readonly`, `forms.responses.readonly` and `tasks` — your
   accounts as test users, then **publish** it. A consent screen left in Testing
   expires refresh tokens after seven days.
3. Credentials → OAuth client ID → **Web application**, with the
   redirect URI DecentAI shows under Settings → Connected apps.
4. Paste the client id and secret into DecentAI under
   **Settings → Connected apps** as provider `google`.

Then, per Google account, anyone: **Agents → Gmail → Credentials →
Connect account**, sign in, done. The credential is named after the
address and can be granted to every other Google agent as well. An
account connected before the shared list grew needs one reconnect. The
`api_base_url` field exists so the tests can point the agent at a
loopback stub; leave it empty.

## Limits, and what is not verified

- Live Gmail behaviour is **not yet verified**: the tests run the agent
  in a real worker against a loopback stub of the Gmail API subset it
  uses. Live verification is pending a real account's token.
- `search.find` makes one metadata call per row (Gmail's list returns
  ids only); `max_results` is capped at 25 per page for that reason.
- Bodies prefer `text/plain`; HTML-only messages are stripped to text.
- Saved attachments must fit the declared MIME list and 20 MB.
- The agent never sends without `draft.send`, and `draft.send` never
  retries an unknown outcome.
- Photos and inline images are not read; they are listed as
  attachments and can be saved.
