# Slack (`slack`)

"What was said in #showroom this morning?" — read as text, oldest
first, with names instead of user ids. "Has anyone answered my question
about the chairs?" — answered from the thread and the conversation
after it. "Reply that the drawings are coming Thursday" — shown to you
word for word, posted as you only when you say so. And "tell me
whenever someone mentions me or messages me" — a watch that a schedule
checks, waking the assistant only when something arrived.

One person's Slack over the Slack Web API, signed in with a **user
token**: the agent sees what that person sees, and a message it sends
is sent in their name.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection: workspace, user name and user id |
| `conversations.list` | 0 | Channels, private channels, group DMs and DMs the person is in; DMs named by the other person |
| `conversations.messages` | 0 | A conversation's recent messages, oldest first, with authors, reply counts and links; `oldest` / `latest` bound the window |
| `conversations.replies` | 0 | A thread's parent and replies, oldest first |
| `messages.search` | 0 | Slack search (`in:#channel`, `from:@name`, `after:`, `on:`), newest first |
| `messages.answered` | 0 | Whether anyone other than the person wrote after a message — in its thread or the conversation — and who |
| `messages.send` | 3 | Text posted as the person to a conversation, optionally into a thread |
| `watch.start` | 1 | Start watching mentions, DMs, or both, from now; conversations can be left out |
| `watch.new` | 1, schedulable | What arrived since the last check, oldest first, each row a mention or a dm; moves the watch forward |

Records: `sent` — every message this agent posted, where, and the `ts`
Slack gave it. `watch` — how far mentions and DMs have been read (a
Slack `ts` cursor), what is watched, and what is left out.

## Rules it keeps

- **Nothing is said without a yes.** Sending is level 3, and the
  instructions are to show the exact text and where it goes first.
- **Nothing is said twice.** A send that gets no answer is reported as
  of unknown outcome and not retried; the conversation is read before
  trying again.
- **Nothing is invented.** Conversation ids, user ids and message `ts`
  values are Slack's own and passed on unchanged; an unknown
  conversation or thread is `not_found`.
- **Answers stay small.** At most 25 messages per answer (20 by
  default), long text clipped, and `more` says when there is more.
  User ids are resolved to display names once per call.
- **Failures have kinds.** `auth` (reconnect the account), `not_found`,
  `rate_limited` (with Slack's Retry-After), `http` (Slack's error code),
  `unknown` (a send whose outcome was lost). Reads are retried once on a
  dropped connection; sends never.

## Watching mentions and DMs

Record a watch once with `watch.start`, then schedule `watch.new` with
`wake_field: messages` every few minutes. Each check:

- searches for messages mentioning the person (`<@USERID>`), newest
  first, keeping those after the cursor;
- reads each DM and group DM after the cursor — at most 40
  conversations per check, most recently touched first; `unchecked`
  counts any beyond that;
- leaves out the person's own messages and any conversation the watch
  skips, returns the rest oldest first (a mention inside a DM is one
  `dm` row), and moves the cursor to the last message it handed on.

A quiet check returns an empty `messages` list, so it costs no model
call.

## Setup

1. At <https://api.slack.com/apps>, **Create New App** → *From scratch*,
   in the workspace it will be used with.
2. Under **OAuth & Permissions**, add the redirect URL the platform
   shows on **Settings → Connected apps** for provider `slack`.
3. Under **User Token Scopes** (not Bot Token Scopes) add:
   `channels:read`, `channels:history`, `groups:read`, `groups:history`,
   `im:read`, `im:history`, `mpim:read`, `mpim:history`, `users:read`,
   `users:read.email`, `search:read`, `chat:write`.
4. Leave **token rotation** off. The platform then holds a long-lived
   user token; this agent never refreshes or mints one.
5. From **Basic Information**, copy the Client ID and Client Secret and
   register them under **Settings → Connected apps** as provider `slack`.
6. Install the agent, open its Credentials tab and connect — for
   example as `demo@sidra.example` in the Sidra Office Supplies
   workspace. Every person connects their own account.

`api_base_url` on the credential exists for the tests' loopback Slack;
leave it empty.

## Limits

- No reactions, edits, deletes, file uploads or scheduled messages.
- `messages.send` posts plain text; Slack formats `*bold*` and `<@U…>`
  mentions the way it does in the Slack app.
- The watch reads DM history, which holds top-level messages and not
  thread replies; a reply in a DM thread is seen only if it mentions the
  person.
- Mentions come from Slack search, which indexes a moment after a
  message is posted. A mention indexed after a later check has already
  moved past it is not reported.
- With more than 40 DMs and group DMs, a check reads the 40 most
  recently touched (by the `updated` field Slack returns, which may not
  track the latest message); `unchecked` says how many were not read.
- Slack's rate limits apply per method; a busy watch with many DMs is
  the thing most likely to meet them, and reports `rate_limited` without
  moving its cursor.
- Live Slack behaviour is verified against a loopback stub of the Web
  API subset it uses; the first real connection is the first live run.
