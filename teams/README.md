# Microsoft Teams (`teams`)

"What did Dana say in our chat?" — read as text, oldest first, with the
system notices left out. "Tell her the drawings are coming Thursday" —
shown to you word for word, sent only when you say so. Later: "Has she
replied?" — answered from the chat itself, because the agent keeps
what it sent.

One person's Microsoft Teams over Microsoft Graph. It rides the same
Microsoft connection as every other Microsoft agent: connect once,
grant the one saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, and whose account it is |
| `chats.list` | 0 | Chats, most recent first, with who is in each; `with` narrows to one person |
| `chats.messages` | 0 | A chat's latest messages as text, oldest first |
| `chats.send` | 3 | Text to a chat, or to one person by address (their one-to-one chat, started if there is none) |
| `chats.replies` | 0 | Whether anyone answered a message this agent sent, and what they said |
| `teams.list` | 0 | The teams the account is in |
| `teams.channels` | 0 | A team's channels |
| `teams.post` | 3 | Text, with an optional subject, to a channel |

Records: `sent` — every chat message and channel post this agent sent,
where it went and when.

## Rules it keeps

- **Nothing is said without a yes.** Sending and posting are level 3,
  and the instructions are to show the exact text first.
- **Nothing is said twice.** A send that gets no answer is reported as
  of unknown outcome and not retried; the chat is read before trying
  again.
- **People are addresses.** A name is refused until it is resolved.
- **Text in, text out.** Teams returns HTML; it is read as text, and
  messages are sent as plain text.

## One consent, no administrator

The agent asks Microsoft only for permissions a person can grant
themselves — reading and writing their own chats, listing their teams
and channels, and posting to a channel — so adding it never turns the
shared Microsoft consent into one that needs an administrator. The
price is that **channel messages cannot be read here**, only posted:
reading them needs `ChannelMessage.Read.All`, which an administrator
must grant. Chats — one-to-one, group and meeting chats — are read in
full.

## Limits

- Channel messages are posted, not read; replies in a channel are not
  followed.
- No @mentions, attachments, reactions or edits.
- No search across all of Teams; a chat is found by who is in it.
- Live Microsoft 365 behaviour is verified against a loopback stub of
  the Graph subset it uses; the first real connection is the first live
  run.
