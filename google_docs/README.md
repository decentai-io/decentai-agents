# Google Docs (`google_docs`)

"What does the Q3 proposal say about delivery, and what did Dana
comment on it?" — the document read paragraph by paragraph, with its
comments and who wrote them. Ask to change the delivery paragraph and
the assistant shows the old and new text side by side; nothing in the
document moves until you say so, and if someone edited the document in
the meantime the change is refused rather than written over theirs.
Ask to be told when someone comments, and a schedule wakes the
assistant only when there is something new.

It rides the same Google connection as Gmail, Google Calendar and
Google Drive: connect once, grant the one saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection and which address it is |
| `docs.find` | 0 | Google Docs whose name or content matches, newest change first, with links |
| `docs.read` | 0 | Title, revision id, and numbered paragraphs (style, text, table cells marked) from `from`, with the total; `next_from` continues |
| `comments.list` | 0 | Open and resolved comments with the text they quote, author, time, and the latest replies |
| `comments.add` | 3 | A comment, optionally quoting words copied exactly from the document |
| `comments.reply` | 3 | A reply to a comment |
| `comments.resolve` | 3 | Resolves a comment, with an optional closing reply |
| `comments.watch` | 1 | Starts watching a document for new comments and replies, from now |
| `comments.new` | 1, schedulable | Comments and replies others wrote since the last check, oldest first, each naming its watch; moves the watch; `more` says a later check continues |
| `edits.propose` | 1 | Records a change to one paragraph: the text as read, the new text, and the document's revision. Nothing in the document changes |
| `edits.apply` | 3 | Applies a proposed edit, refused if the document changed since |
| `edits.discard` | 1 | Marks a proposed edit discarded |

Records: `edit` (each proposed change and what became of it) and
`watch` (how far a document's comments have been read).

## Rules it keeps

- **Propose, then apply.** An edit is a record first. `edits.apply` is
  a separate level-3 call, and it reads the document again before it
  writes: the paragraph's range comes from that fresh read, never from
  the proposal.
- **A changed document is refused, not overwritten.** The edit carries
  the revision it was proposed against; if the document's revision is
  different, nothing is sent. The write itself goes with
  `writeControl.requiredRevisionId`, so a change landing between that
  read and the write is refused by Google too. A refused edit is marked
  `refused` with the reason, and must be proposed again.
- **One paragraph, its style kept.** The words between the paragraph's
  start and its own newline are replaced; the paragraph, its style and
  everything around it stay. A paragraph holding an image, a chip or
  another object is not rewritten here.
- **No answer is not a success.** An apply that gets no answer is
  marked `unknown` and never sent again; the person looks at the
  document.
- **Comments others see are level 3.** Adding, replying and resolving
  wait for a yes. A quote must be words the document actually holds.
- **Only what others wrote wakes the assistant.** Comments and replies
  written by the connected account itself are never reported by
  `comments.new`.

## Watching for comments

The assistant records one watch per document and schedules
`comments.new` with `wake_field: comments`. The watch is a cursor in
Google's own clock: the creation time of the newest comment or reply
seen, plus the ids created at exactly that time, so two in the same
moment are neither lost nor shown twice. Drive filters comments by
*modified* time (a new reply moves its comment), so each check asks for
everything modified since the cursor and decides what is new by
creation time. A document with no comments yet starts from its last
modification time, which every later comment comes after.

## Limits

- **Comments made through the API are not anchored.** Drive accepts a
  `quotedFileContent` and shows it with the comment, but a comment
  created through the API is not attached to a range in the Docs
  editor: it appears in the comment list, not highlighted on the words
  it quotes. Google does not offer a way to anchor one.
- Comment authors come back as display names, not addresses; Drive
  marks the account's own comments with `me`, which is how they are
  told apart.
- Google guarantees a revision id for 24 hours only, so a proposal
  older than a day may be refused even if nobody touched the document;
  propose it again.
- Any change to the document moves its revision — including applying a
  different edit — so two proposals against the same revision cannot
  both be applied: the second is refused and proposed again.
- Paragraphs are read from the body, table cells included; headers,
  footers, footnotes and a table of contents are not.
- The `drive` and `documents` scopes are restricted or sensitive Google
  scopes: a public app needs Google's verification before people outside
  test users can connect.
- Live Google behaviour is verified against a loopback stub of the
  Docs and Drive subset it uses; the first real connection is the first
  live run. In particular not yet seen live: the exact error Google
  returns for a stale `requiredRevisionId` (the client treats a 400
  mentioning the revision, or `FAILED_PRECONDITION`, as a refusal), and
  whether adding a comment moves a document's revision id.
