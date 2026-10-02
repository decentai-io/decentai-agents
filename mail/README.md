# Mail (`mail`)

Someone wants the assistant to read their mail and has never heard of
a developer console. They type their address and an app password, and
ask for the latest message from a supplier.

Any mail account, over the protocols mail has always spoken: read over
IMAP, sent over SMTP, both encrypted, from where this agent runs to
the account's own mail server. Nothing is registered with a provider
and nobody reviews anything.

It does what the Gmail and Outlook agents do, and the assistant uses
the three alike.

## Connecting an account

| Field | What goes in it |
|---|---|
| Email address | the account's address |
| Provider | `gmail`, `yahoo`, `icloud`, `fastmail`, or `other` |
| App password | a password made in the provider's security settings for one program to use — not the account's own, which most providers refuse here |
| IMAP server, SMTP server | for `other` only: the names of the provider's mail servers |

| Provider | Where an app password is made |
|---|---|
| Gmail | 2-Step Verification on, then `https://myaccount.google.com/apppasswords` |
| Yahoo | Account security, Generate app password |
| iCloud | `https://account.apple.com`, Sign-In and Security, App-Specific Passwords |
| Fastmail | Settings, Privacy & Security, App passwords |

Microsoft's accounts are not here: Outlook.com takes no password over
IMAP, and the Outlook agent is the way in.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Signs in and opens the inbox; when that fails, says why and where an app password is made |
| `account.list` | 0 | The connected accounts, by address |
| `search.find` | 0 | Messages in one folder, newest first: by sender, recipient, subject, words anywhere, dates, unread |
| `search.thread` | 0 | One conversation whole, from the inbox and from what was sent, with its attachments listed |
| `search.save_attachment` | 2 | One attachment as a platform file |
| `draft.reply`, `draft.compose` | 1 | A draft, kept by the platform and shown to the person. Nothing is sent |
| `draft.send` | 3 | Sends a draft, on the person's go-ahead. `sent` only when the mail server took it; `unknown` when it was handed over and no answer came, and never tried again |
| `draft.discard` | 1 | Marks a draft discarded |
| `watch.await_reply`, `watch.check` | 1 | Remembers a conversation and says whether somebody else has written since. Schedulable |
| `watch.inbox`, `watch.new_mail` | 1 | Remembers how far the inbox was read and hands on what arrived since, once. Schedulable |

## What differs from Gmail's own agent

- **No query language.** `search.find` takes a sender, a subject, words
  and dates as separate inputs.
- **One folder a search.** The account's archive of everything where
  it has one (Gmail's All Mail), else the inbox; `folder: sent` for
  what was sent.
- **A conversation is rebuilt.** A mail server keeps messages, not
  conversations: a conversation is the messages that name one another
  in `References`. Ids are the messages' own `Message-ID`s.
- **A draft lives on the platform,** not in the account's Drafts
  folder: what is sent is what the person was shown.
- **A copy of what was sent** is put in the Sent folder where the
  server kept none itself.
- **No link to a message:** a mail server has no web page for one.

## Where it connects

The mail servers of the four providers it knows by name, each on the
port mail is read on (993) and the ports it is sent on (465, 587), and
for another provider the servers the account names, on the same ports.
Every connection is opened with the SDK's `Tunnel`, so where the
platform confines agents it goes through the platform's proxy, which
opens those hosts on those ports and nothing else. The proxy passes
the bytes and reads none of them: the connection is encrypted between
this agent and the mail server.

## What it never does

It deletes no mail, moves none and marks none as read: folders are
opened to be read. The one thing it writes to a mailbox is the copy of
what it sent.
