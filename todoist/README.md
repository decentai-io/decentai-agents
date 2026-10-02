# Todoist (`todoist`)

You keep your to-dos in Todoist. Ask what is due today or overdue, and
the assistant reads your list. Tell it to add a task to a project with
a date and a priority, and it files it there, or tells you the project
names you really have. Tick things off in the Todoist app, and a
schedule carries them into your other lists without you asking.

Reads tasks by project, section, label or a Todoist filter query.
Creates, updates, completes and reopens tasks, and creates projects,
as ordinary changes in the person's own list. Deleting a task and
commenting on one are level-3 actions on the user's explicit go-ahead,
because a comment is seen by everyone the project is shared with. A
watch on completions lets a schedule wake the assistant only when
something was ticked off.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection: email, full name, time zone |
| `projects.list` | 0 | Active projects, with parent, shared, Inbox and link |
| `projects.create` | 1 | A new project, optionally under an existing parent |
| `sections.list` | 0 | Sections of one project or of all |
| `labels.list` | 0 | The person's labels |
| `tasks.list` | 0 | Open tasks by project, section or label, or by a filter query (`today \| overdue`); at most 25 per answer, `cursor` continues |
| `tasks.get` | 0 | One task with its description, comment count and whether it is completed |
| `tasks.create` | 1 | Adds a task: content, description, project, section, `due_string` or `due_date`, priority, labels |
| `tasks.update` | 1 | Changes content, description, due, priority or labels |
| `tasks.complete` | 1 | Ticks a task off; a recurring one moves to its next date |
| `tasks.reopen` | 1 | Puts a completed task back |
| `tasks.delete` | 3 | Deletes a task and its sub-tasks |
| `comments.add` | 3 | Comments on a task; the project's collaborators see it |
| `sync.watch` | 1 | Starts a watch on completions, in all projects or one |
| `sync.completed` | 1 | What was completed since the last check, oldest first; schedulable, `wake_field: completed` |

Records: `watch` (one per completion watch: how far completions have
been handed on, and which project counts).

## Names, never guesses

A project, section or label is always one the account already has. A
name that matches nothing is refused with the real names listed, as
`kind: invalid` with `known`. Todoist itself would quietly create an
unknown label or drop the task in the Inbox. Names match in any case,
and an id a list returned works too.

**Priority** is the app's word: `P1` is the most urgent, `P4` the
default. Todoist's API numbers them the other way round (API priority 4
is the app's P1); the agent converts both ways, so the assistant never
sees the API's numbers.

**Due dates**: `due_string` takes Todoist's own words (`tomorrow`,
`every Monday`, `no date` to clear), and `due_date` takes `YYYY-MM-DD`.
A row's `due_date` is Todoist's own: a bare date, or a date and time for
a timed task.

## Keeping Todoist and Tasks in step

Agents cannot call each other. The assistant does the carrying, and the
manifest's instructions tell it how:

- **Copying a task** across means reading it from one agent and creating
  it in the other, with the same title and due date. It names where the
  task came from: the Todoist link as the Tasks item's source, or the
  Tasks item in the Todoist description.
- **Carrying completions** from Todoist uses one `sync.watch`, then a
  schedule on `sync.completed` with `wake_field: completed`. Each wake
  carries only what was completed since the last check. The assistant
  finds the matching open Tasks item and marks it done with the Todoist
  link as evidence. A quiet check costs no model call.

## How the completion watch counts

The watch keeps a cursor in Todoist's own clock: the completion time of
the newest completed task handed on, plus the ids of the tasks
completed at exactly that time. A task completed in the same instant as
the last one is therefore neither lost nor shown twice. A new watch
starts at the newest completion of the past day, or at now. A check
asks one range, from the cursor to now, reads every page, sorts oldest
first, and hands on at most `max_results`. The cursor moves only
through what it handed on, and `more` says the next check continues.
Todoist answers at most three months per request, so a watch left
alone longer catches up three months per check.

## Setup

1. In the [Todoist App Management console](https://developer.todoist.com/appconsole.html),
   create an app.
2. Set its **OAuth redirect URL** to the one the platform shows under
   **Settings → Connected apps** for the provider `todoist`.
3. Copy the app's **client id** and **client secret** into
   **Settings → Connected apps** as provider `todoist`.
4. Install this agent, open its Credentials tab, and **Connect**. The
   sign-in asks for `data:read_write` and `data:delete`.

Todoist tokens **do not expire** and come with no refresh token. The
platform keeps the token until the person disconnects the account or
revokes the app in Todoist's settings (Integrations). A revoked token
answers `kind: auth`, and the fix is to reconnect.

## Failures

- `auth`: not connected, or the token was revoked. Reconnect.
- `not_found`: no such task, or it was deleted.
- `invalid`: a name that does not exist, a bad date, or a query combined
  with project/section/label. Nothing was sent.
- `http`: Todoist refused, for example an invalid filter query. The
  message quotes Todoist.
- `unknown`: a write got no answer. It is never retried; check Todoist.

Reads are retried once on a dropped connection; writes never are.

## Limits, and what is not verified

- Live Todoist behaviour is **not yet verified**. The tests run the
  agent in a real worker against a loopback stub of the API v1 subset
  it uses. The endpoint paths and field names follow Todoist's official
  SDKs, not a live account.
- Tasks cannot be moved between projects, assigned to someone, or given
  reminders or durations here. Assigning to someone else would be a
  level-3 action and is not offered.
- Only personal labels are checked. Shared labels from shared projects
  are not listed and are refused by name.
- The comment count is the number of comments read back, one request
  per `tasks.get`.
- Links are built as `https://app.todoist.com/app/task/<id>`; the API
  returns none.
- A new watch without `since` looks back one day for its starting
  point.
