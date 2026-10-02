# Word Online (`word_online`)

"What does the Harbourline proposal say about delivery, and what did
Dana comment on it?" — the Word document in your OneDrive read
paragraph by paragraph, with its comments and the paragraph each sits
on. Ask to change the delivery paragraph and the assistant shows the
old and new text side by side; the file is saved only when you say so,
and only if nobody saved it in the meantime. Ask to be told when the
document changes or gets a comment, and a schedule wakes the assistant
only then.

It rides the same Microsoft connection as Outlook, Microsoft Calendar,
OneDrive and Teams: connect once, grant the one saved account to each.

## How it reads a Word document

**Microsoft Graph has no API for the content of a Word document.** It
cannot list paragraphs, read or add comments, or edit text. What it has
is the file. So this agent:

- downloads the `.docx` (`/me/drive/items/{id}/content`) and reads its
  paragraphs with python-docx;
- reads comments straight from the package's `word/comments.xml`, and
  places each on the paragraph holding its range start;
- for an edit, changes that one paragraph in the file and uploads the
  whole file again (`PUT .../content`) with `If-Match` on the eTag the
  edit was proposed against.

It cannot add, answer or resolve comments: Graph offers no way to, and
writing comment XML into a file other people may have open is not a
safe substitute.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection and which address it is |
| `docs.find` | 0 | `.docx` files matching a query, newest change first |
| `docs.read` | 0 | Numbered paragraphs (style name, text, table cells marked) from `from`, with the total, the file's eTag and when and by whom it was last saved |
| `comments.list` | 0 | The comments the file holds: author, date, text, and the paragraph each sits on |
| `edits.propose` | 1 | Records a change to one paragraph: the text as read, the new text, and the file's eTag. Nothing changes |
| `edits.apply` | 3 | Saves a proposed edit into the file, refused if the file changed since |
| `edits.discard` | 1 | Marks a proposed edit discarded |
| `changes.watch` | 1 | Starts watching a document for saves and new comments, from now |
| `changes.new` | 1, schedulable | For each watch, whether the file was saved since (who, when) and its new comments; moves the watch |

Records: `edit` (each proposed change and what became of it) and
`watch` (each watched document as last seen).

## Rules it keeps

- **Propose, then apply.** An edit is a record first; `edits.apply` is
  a separate level-3 call.
- **A changed file is refused, not overwritten.** Applying first asks
  Graph for the item: if its eTag is not the one recorded, the edit is
  refused, naming who saved the file and when, and nothing is
  downloaded. Otherwise the file is downloaded fresh, the paragraph is
  checked to still read as proposed, and the upload carries
  `If-Match: <eTag>` — a save by someone else between the check and the
  upload is refused by OneDrive (412). A refused edit is marked
  `refused` with the reason and must be proposed again.
- **One paragraph, its formatting kept.** The paragraph stays, with its
  style and its first run's formatting; its other runs are cleared and
  the new words go in that first run's formatting. Comment marks and
  bookmarks stay where they are, so a comment on the paragraph is still
  on it. A paragraph with tracked changes, a picture, an embedded object
  or an equation is not rewritten here.
- **The whole file goes back as it came.** python-docx saves every part
  of the package it did not change — comments, styles, headers.
- **Size is said, not discovered.** A document over 25 MB is refused
  for editing with its size, before anything is downloaded — the
  platform's ceiling for a file an agent handles. Reading downloads
  documents up to 25 MB, which stay inside the worker.
- **No answer is not a success.** An upload that gets no answer is
  marked `unknown` and never sent again.

## Watching a document

The assistant records one watch per document and schedules
`changes.new` with `wake_field: changes`. The watch holds the file's
eTag, its last-modified time and a fingerprint of each comment. A check
asks Graph for the item's metadata only and downloads the file only
when the eTag moved. Comments are fingerprinted by author, date and
text rather than by id, because Word renumbers comment ids when it
saves.

What is not news: a save by the connected account itself (such as an
applied edit), unless it carried someone else's new comment; and a
comment whose author is the account's own display name — Word records a
comment's author as a name, not an address, so the name is what is
compared.

## Limits

- `.docx` only; `.doc`, `.docm` and files in shared drives or other
  people's OneDrives are not read.
- Paragraphs come from the body, table cells and content controls
  included; headers, footers, footnotes and text boxes are not read.
- Comment replies and "resolved" state (kept by newer Word in
  `commentsExtended.xml`) are not read: every comment is listed as a
  comment.
- If someone else saves and the account saves over it before a check,
  the check sees only the account's save.
- Live Microsoft behaviour is verified against a loopback stub serving
  real `.docx` files; the first real connection is the first live run.
  Not yet seen live: whether OneDrive honours `If-Match` on
  `PUT .../content` for every account type (the documented behaviour is
  412 on a mismatch), how a file open for co-authoring in Word for the
  web answers an upload, and how the comments of a document last saved
  by Word for the web are laid out in its package.
