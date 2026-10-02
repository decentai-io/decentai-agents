# Google Drive (`google_drive`)

"Where is the Riverside lease, and who can open it?" — found by name or
content, with everyone who has access: the owner, the people invited,
and whether the whole organization or anyone with the link can open
it. Ask for a copy and it comes into the platform, where Documents can
read it. Ask to put a file in Contracts and share it with Dana, and
each step waits for your go-ahead.

OneDrive's twin over the Drive API: the same functions and the same
rules. It rides the same Google connection as every other Google
agent: connect once, grant the one saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, and how full the drive is |
| `files.search` | 0 | Files and folders matching a query, by name or content |
| `files.list` | 0 | What is in a folder, folders first, by path or id |
| `files.shared_with_me` | 0 | Files others shared, newest share first, with who shared them |
| `files.get` | 0 | One item's details |
| `files.download` | 2 | A copy into the platform; Docs, Sheets and Slides come in as Word, Excel, PowerPoint — or PDF |
| `files.upload` | 3 | A platform file into a Drive folder; never a duplicate name unless told |
| `files.create_folder` | 3 | A new folder |
| `files.move` | 3 | Renames, moves, or both |
| `files.delete` | 3 | To the trash, from which it can be restored |
| `sharing.permissions` | 0 | Who can open an item, and how |
| `sharing.invite` | 3 | Gives named people read or write access; Google emails them |
| `sharing.link` | 3 | Opens the item to the organization, or to anyone when asked, and returns its link |
| `sharing.revoke` | 3 | Removes one permission |

Records: `share` — every grant and revocation this agent made.

## Rules it keeps

- **Nothing in the drive changes without a yes.** Upload, folder,
  move, rename, trash, share and un-share are all level 3.
- **Delete means the trash.** Nothing here deletes for good.
- **An upload does not make a second file of the same name.** Drive
  would allow it; the agent renames to "name 1.ext" instead, or, when
  asked (`on_conflict: replace`), uploads a new version of the file
  that is there.
- **A link is for the organization.** "Anyone with the link" is made
  only when the user asks for it in those words. A personal Gmail
  account has no organization, so the agent asks rather than widening
  it on its own.
- **The owner's access, and access inherited from a shared drive, are
  not revoked here.**
- **People are addresses.** A name is refused until it is resolved.

## Where it differs from OneDrive

Drive has no paths: a folder the user names is found one segment at a
time, and each row says the name of the folder it sits in rather than a
full path. Only Google's own formats convert to PDF — a Word file
uploaded to Drive comes in as Word. And a file shared with the account
is addressed by its id alone.

## Limits

- Files up to 25 MB each way, in one request; Google limits exports of
  Docs, Sheets and Slides to 10 MB.
- My Drive and files shared with the account; shared drives are not
  browsed.
- The `drive` scope is a restricted Google scope: a public app needs
  Google's verification before people outside test users can connect.
- Live Google behaviour is verified against a loopback stub of the
  Drive subset it uses; the first real connection is the first live
  run.
