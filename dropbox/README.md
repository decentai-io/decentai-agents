# Dropbox (`dropbox`)

"Where is the Riverside lease, and who can open it?" — found by name or
content, with everyone who has access: the owner, the people invited,
anyone reaching it through a shared folder, and every link and how far
it reaches. Ask for a copy and it comes into the platform, where
Documents can read it. Ask to put a file in Contracts and share it with
Dana, and each step waits for your go-ahead. Ask to hear when invoices
land in a folder, and a schedule wakes the assistant only when one did.

The Google Drive and OneDrive agents' twin over Dropbox API v2: the same
functions and the same rules, plus a change watch.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, the team it belongs to, and how full the account is |
| `files.search` | 0 | Files and folders matching a query, by name or content |
| `files.list` | 0 | What is in a folder, folders first, by path or id |
| `files.shared_with_me` | 0 | Files others shared, newest share first, with who shared them |
| `files.get` | 0 | One item's details |
| `files.download` | 2 | A copy into the platform; Word and PowerPoint can come in as PDF |
| `files.upload` | 3 | A platform file into a Dropbox folder; never overwrites unless told |
| `files.create_folder` | 3 | A new folder |
| `files.move` | 3 | Renames, moves, or both |
| `files.delete` | 3 | To Dropbox's deleted files, from which it can be restored |
| `sharing.permissions` | 0 | Who can open an item, and how |
| `sharing.invite` | 3 | Gives named people read or write access; Dropbox emails them |
| `sharing.link` | 3 | A link for the team, or for anyone when asked |
| `sharing.revoke` | 3 | Removes one person, group or link |
| `changes.watch` | 1 | Starts watching a folder and everything under it, from now |
| `changes.new` | 1, schedulable | What changed since the last check; moves the watch forward |

Records: `share` — every grant and revocation this agent made; `watch`
— the folders watched, and Dropbox's cursor for each.

## Rules it keeps

- **Nothing in the Dropbox changes without a yes.** Upload, folder,
  move, rename, delete, share and un-share are all level 3.
- **Delete means Dropbox's deleted files.** They can be restored for as
  long as the account's plan keeps them; nothing here deletes for good.
- **An upload never overwrites by default.** A name already taken gets
  Dropbox's own numbered name ("name (1).ext"); replacing is asked for
  by name (`on_conflict: replace`), and Dropbox keeps the old version.
- **A link is for the team.** "Anyone with the link" is made only when
  the user asks for it in those words. A personal account has no team,
  so the agent asks rather than widening it on its own. An item that
  already has a link gets that link changed, not a second one.
- **The owner's access, and access that comes from a shared parent
  folder, are not revoked here.**
- **People are addresses.** A name is refused until it is resolved.
- **A file too large to carry is refused before it is fetched,** with
  its size named.

## Watching a folder

The assistant records one watch (`changes.watch`) and schedules
`changes.new` with `wake_field: changes` every few minutes. A check that
finds nothing costs no model call. The watch holds Dropbox's own
`list_folder` cursor, so each change is handed on once: files added or
changed (Dropbox does not say which), and anything deleted, with who
changed a file when it sits in a shared folder. Dropbox pages changes
25 at a time; `more` says the next check continues. If Dropbox declares
the cursor stale, the watch starts again from now and is listed under
`reset` — whatever changed in between was not seen.

## Where it differs from the drive twins

Dropbox has real paths, so every function takes a path ("/Contracts/
lease.pdf") as well as an id ("id:…"), and each row carries both. A
Word or PowerPoint file converts to PDF through Dropbox's preview;
spreadsheets do not (Dropbox previews them as HTML). A Dropbox Paper
document has no bytes of its own and comes in exported. A file shared
with the account but never added to it is fetched through its link.
Dropbox keeps no web address for an item, so `link` is filled only
where Dropbox gave one.

## Setup

1. In the [Dropbox App Console](https://www.dropbox.com/developers/apps),
   create an app with **Scoped access** and **Full Dropbox** access.
2. Under **Permissions**, tick `account_info.read`,
   `files.metadata.read`, `files.content.read`, `files.content.write`,
   `sharing.read` and `sharing.write`, and submit.
3. Under **Settings → OAuth 2 → Redirect URIs**, add the redirect URI
   DecentAI shows under Settings → Connected apps.
4. Paste the app key and app secret into DecentAI under
   **Settings → Connected apps** as provider `dropbox` (client id and
   client secret).

Then, per Dropbox account, anyone: **Agents → Dropbox → Credentials →
Connect account**, sign in, done. The platform asks Dropbox for offline
access, so the connection keeps refreshing.

Leave `api_base_url` empty; it exists for the tests.

## Limits

- Files up to what the platform stores (25 MB) each way.
- The account's own Dropbox and files shared with it; team spaces are
  browsed only as far as they appear in the account's Dropbox.
- A new app works for its developer and up to 500 users before Dropbox's
  production review.
- Live Dropbox behaviour is verified against a loopback stub of the API
  subset it uses; the first real connection is the first live run.
