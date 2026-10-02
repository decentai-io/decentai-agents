# Box (`box`)

"Where is the Riverside lease, and who can open it?" — found by name or
content, with everyone who has access: the owner, the collaborators,
anyone reaching it through a folder above, and whether its shared link
opens to the company or to anyone. Ask for a copy and it comes into the
platform, where Documents can read it. Ask to put a file in Contracts
and share it with Dana, and each step waits for your go-ahead. Ask to
hear when something is uploaded, and a schedule wakes the assistant
only when something was.

The Google Drive, OneDrive and Dropbox agents' twin over Box API 2.0:
the same functions and the same rules, plus a change watch.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, the company it belongs to, and how full the account is |
| `files.search` | 0 | Files matching a query, by name or content |
| `files.list` | 0 | What is in a folder, folders first, by path or id |
| `files.shared_with_me` | 0 | Files and folders others own and shared, with their owner |
| `files.get` | 0 | One item's details |
| `files.download` | 2 | A copy into the platform; Office files can come in as PDF |
| `files.upload` | 3 | A platform file into a Box folder; never a duplicate name unless told |
| `files.create_folder` | 3 | A new folder |
| `files.move` | 3 | Renames, moves, or both |
| `files.delete` | 3 | To the Box trash, from which it can be restored |
| `sharing.permissions` | 0 | Who can open an item, and how |
| `sharing.invite` | 3 | Makes named people collaborators, viewer or editor; Box emails them |
| `sharing.link` | 3 | The shared link, for the company or for anyone when asked |
| `sharing.revoke` | 3 | Removes one collaborator, or the link |
| `changes.watch` | 1 | Starts watching the account's changes, from now |
| `changes.new` | 1, schedulable | What changed since the last check, oldest first; moves the watch forward |

Records: `share` — every grant and revocation this agent made; `watch`
— Box's stream position, and the events most recently handed on.

## Rules it keeps

- **Nothing in the Box changes without a yes.** Upload, folder, move,
  rename, trash, share and un-share are all level 3.
- **Delete means the trash.** A folder goes with what is in it; nothing
  here deletes for good.
- **An upload does not make a second file of the same name.** Box
  refuses a taken name, so the agent checks first and uses "name 1.ext",
  or, when asked (`on_conflict: replace`), uploads a new version of the
  file that is there.
- **A link is for the company.** "Anyone with the link" is made only
  when the user asks for it in those words. A personal account has no
  company, so the agent asks rather than widening it on its own. Only a
  file's link can allow editing.
- **The owner's access, and access given on a folder above, are not
  revoked here.**
- **People are addresses.** A name is refused until it is resolved.
- **A file too large to carry is refused before it is fetched,** with
  its size named.

## Watching for changes

The assistant records one watch (`changes.watch`) and schedules
`changes.new` with `wake_field: changes` every few minutes. A check that
finds nothing costs no model call. The watch holds Box's own stream
position for the `changes` stream and hands on the events about files
and folders — uploaded, created, moved, copied, renamed, trashed,
restored, a version made current, a link made, changed or removed, a
collaborator added or removed — oldest first, with who did it. Box
says it may deliver an event twice; the watch remembers the ids it
handed on and never repeats one. A full chunk sets `more`. A watch
covers the whole account; the rows' `folder` narrows it.

## Where it differs from the drive twins

Box addresses files and folders separately; a bare id is tried as a
file, then as a folder. Box has no "shared with me" list — what others
share with the account sits at its top level, so that is where the
agent looks, and names each item's owner. An invitation carries no
personal message. A PDF of an Office file is Box's own rendering, made
on first request; if Box is still making it, the agent says to try
again shortly.

## Setup

1. In the [Box Developer Console](https://app.box.com/developers/console),
   create a **Custom App** with **User Authentication (OAuth 2.0)**.
2. Under **Configuration → OAuth 2.0 Redirect URI**, add the redirect
   URI DecentAI shows under Settings → Connected apps.
3. Under **Application Scopes**, tick **Read all files and folders
   stored in Box** and **Write all files and folders stored in Box**
   (`root_readwrite`), and save.
4. Paste the client id and secret into DecentAI under
   **Settings → Connected apps** as provider `box`.

Then, per Box account, anyone: **Agents → Box → Credentials → Connect
account**, sign in, done. Box issues a new refresh token each time the
platform refreshes; the platform keeps the newest.

Leave `api_base_url` empty; it exists for the tests. A company Box may
require an administrator to authorize the app before anyone can connect.

## Limits

- Files up to what the platform stores (25 MB) each way.
- Search returns files, not folders.
- Live Box behaviour is verified against a loopback stub of the API
  subset it uses; the first real connection is the first live run.
