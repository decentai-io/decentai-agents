# Outlook (`outlook`)

Dana at Harbourline asks for a quotation for twenty desks. Ask the
assistant to find her email, draft the reply, and remind you on Thursday
if she has not answered. The draft appears in Outlook before it goes
anywhere; the send needs your go-ahead; and Thursday's reminder fires
only if the conversation is still quiet.

The Gmail agent's twin for a Microsoft 365 mailbox, over Microsoft
Graph: finds and reads conversations, prepares replies and new
messages as Outlook drafts, sends a draft only on explicit
authorization, saves attachments as files for other agents, watches a
conversation for a reply so a schedule can remind the user only while
the reply is still missing, and watches the inbox so a schedule can
wake the assistant only when mail has arrived.

One Microsoft account per credential, connected by signing in: the
platform runs the consent, keeps the refresh token and refreshes it;
the agent receives a short-lived access token and the address. A
person may connect more than one: `account.list` names them, every
function that reads or writes mail takes an optional `account` (the
address), and a draft, a watch and an inbox watch remember the account
they were made on. With none named, the person's default answers, or
the only account; a shared mailbox is a connected account granted to
the agent org-wide.

## Functions

The same as the Gmail agent's, with the same levels and the same
records — a chat that learned one can drive the other:

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection and which address it is |
| `account.list` | 0 | The connected accounts by address, and which is the default |
| `search.find` | 0 | KQL search → one row per message, most relevant first, with the conversation id as `thread_id` and a link; `page_token` continues |
| `search.thread` | 0 | One conversation whole: senders, dates, body text (cut at `max_body_chars`), attachment list with ids |
| `search.save_attachment` | 2 | One attachment saved as a file (`file_ref`) for Documents or Spreadsheets to read |
| `draft.reply` | 1 | A reply prepared as an Outlook draft in the conversation, to the last sender unless `to` is given. Nothing is sent |
| `draft.compose` | 1 | A new message prepared as an Outlook draft. Nothing is sent |
| `draft.send` | 3 | Sends a prepared draft. Succeeds only when Outlook confirms the message left Drafts |
| `draft.discard` | 1 | Deletes the Outlook draft and marks the record discarded |
| `watch.await_reply` | 1 | Records that the user waits for a reply on a conversation, from now |
| `watch.check` | 1, schedulable | Checks waiting watches; `unanswered` lists conversations still without a reply from anyone else |
| `watch.inbox` | 1 | Starts watching the inbox from now (or from `since`); `only_from` / `skip_from` name the senders that count, as addresses or `@domain`s |
| `watch.new_mail` | 1, schedulable | What arrived since the last check, oldest first, one row per message naming its watch; moves the watch forward; `more` says a later check continues |

Records: `draft` (what was prepared and what became of it), `watch`
(conversations awaited) and `inbox` (how far the inbox has been read).
Files: `attachment` (what was saved out of mail).

## Watching the inbox

The assistant records one inbox watch and schedules `watch.new_mail`
with `wake_field: messages` every few minutes. A check that finds
nothing costs no model call; a check that finds mail wakes the
assistant with the rows, and the standing instruction in that chat
says what to do with each. The watch is a timestamp cursor in Graph's
own clock — the time of the newest message handed on, plus the ids
that sit exactly on that second — so a second mail in the same second
is neither lost nor shown twice. Mail from the account itself never
counts; `only_from` and `skip_from` keep a newsletter from costing a
wake. The cursor moves when the rows are handed on, so a wake the
assistant failed to act on is not offered again.

## Search syntax

`query` is Microsoft's KQL: plain words, `from:northlight`,
`to:dana`, `subject:quotation`, `received>=2026-09-01`,
`hasAttachments:true`, joined with `AND` / `OR`. Graph ranks search
results by relevance, so rows come most relevant first rather than
newest first; `received>=` narrows them to a period.

## What differs from Gmail under the hood

- A "thread" is Graph's `conversationId`; a message's id is Graph's
  immutable id (the agent asks for immutable ids, so a message keeps
  its id when Outlook moves it between folders).
- Graph's send answers *accepted* and nothing more. The agent then
  reads the same message back: `isDraft` turned false is Outlook's
  confirmation, and anything else is reported as **unknown** and never
  retried here.
- A reply is Outlook's own `createReply` — the quoted history, the
  Re: subject and the conversation come from Outlook — with the body
  and, when needed, the recipients set afterwards.
- Inline images are not listed as attachments; real attachments are.

Send outcomes are worded exactly as the Gmail agent words them:
**sent**, **error** (the draft stays `prepared`), **unknown** (marked,
never resent by this function; check the Sent folder).

A grant Microsoft no longer accepts is reported as `kind: auth` with
the instruction to reconnect; the credential shows *needs reconnect*
on the Credentials page.

## Setup

Once per organization, by an administrator, in the Microsoft Entra
admin center:

1. App registrations → New registration. Name it, choose *accounts in
   any organizational directory and personal Microsoft accounts*, and
   add a **Web** redirect URI: the one DecentAI shows under
   Settings → Connected apps.
2. Certificates & secrets → a client secret (copy the value at once).
3. API permissions → Microsoft Graph, delegated — the list every
   Microsoft agent in the catalog shares: `openid`, `email`,
   `offline_access`, `User.Read`, `Mail.ReadWrite`, `Mail.Send`,
   `Calendars.ReadWrite`, `Files.ReadWrite.All`, `Chat.ReadWrite`,
   `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Send`
   and `Tasks.ReadWrite`. Grant admin consent for your tenant so your
   own users see no prompt.
4. Paste the application id and the secret into DecentAI under
   **Settings → Connected apps** as provider `microsoft`.

Then, per mailbox, anyone: **Agents → Outlook → Credentials → Connect
account**, sign in, done. The credential is named after the address
and can be granted to every other Microsoft agent. An account connected
before the shared list grew needs one reconnect.
The `api_base_url` field exists so the tests can point the agent at a
loopback stub; leave it empty.

## Limits, and what is not verified

- The tests run the agent in a real worker against a loopback stub of
  the Graph subset it uses; live Microsoft 365 behaviour is verified
  by hand against a real mailbox, not by the suite.
- `search.find` is capped at 25 rows per page; Graph's search
  returns at most 250 in total.
- Bodies are requested as text; Outlook renders HTML mail to text on
  its side, and the agent strips tags only if a server ignores that.
- One mailbox: the signed-in user's. Shared mailboxes are reached by
  connecting them as their own account.
