# Google Tasks (`google_tasks`)

You tick tasks off in Google Tasks, beside Gmail and Calendar, and keep
the rest of your work in Tasks. Ask for what is open and what is due
this week; add a task, or a subtask, from the chat; and have the
assistant check each morning what you completed in Google Tasks and
mark the matching Tasks items done — it wakes only when something was
completed.

Microsoft To Do's twin, over the Google Tasks API: the same functions,
the same record and the same rules, for a person's own Google Tasks
lists. What Google Tasks does not have — importance, reminders — this
agent does not offer; what it has instead — subtasks — it does.

It uses the same Google connection as Gmail, Google Calendar and Google
Drive. Connect once, grant the one saved account to all of them. The
connection asks for the `tasks` scope.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection reaches the account's tasks |
| `lists.list` | 0 | The account's lists with their ids, the default list first |
| `lists.create` | 1 | Makes a new list |
| `tasks.list` | 0 | A list's tasks (the default list when none is named), open / completed / all, due before a date, most recently changed first; `page_token` continues |
| `tasks.get` | 0 | One task whole: its notes and its subtasks |
| `tasks.create` | 1 | Adds a task: title, due date, note; `parent` makes it a subtask |
| `tasks.update` | 1 | Changes title, due date or note; an empty due clears it |
| `tasks.complete` | 1 | Ticks a task off |
| `tasks.reopen` | 1 | Marks a completed task as needing action again |
| `tasks.delete` | 3 | Deletes a task and its subtasks; waits for the user's go-ahead |
| `sync.watch` | 1 | Starts watching one list, or every list, from now (or from `since`) |
| `sync.completed` | 1, schedulable | Tasks completed since the last check, oldest first, each with its list name and watch; moves the completion cursor; `more` says a later check continues |
| `sync.changed` | 1, schedulable | Tasks created or changed since the last check, completions included, each with its status; moves the change cursor |

Records: `watch` (how far the lists have been read, for completions and
for changes). The tasks themselves live in Google Tasks, not on the
platform.

## Why a personal task is level 1

A task in the person's own Google Tasks is private and inert until they
act on it, like a Gmail draft. Deleting one cannot be taken back, so it
is level 3 and pauses for approval in a chat below that trust.

## Keeping in step with Tasks

Agents cannot call each other; the assistant carries a task across,
and the manifest tells it how.

- **Google Tasks into Tasks**: `tasks.get` here, then the Tasks agent's
  `tasks.create` with `source` exactly `google_tasks:<task_id>`.
- **Tasks into Google Tasks**: `tasks.create` here, with the note ending
  `Tasks item: <task_ref>`.
- **What was ticked off in Google Tasks**: one `sync.watch`, and a
  schedule on `sync.completed` with `wake_field: completed`. Each row
  carries the `task_id` and the note, which is what finds the Tasks
  item — by `source`, or by the `task_ref` in the note — and the
  assistant marks it done with the completion date as evidence.

## Watching

Each watch keeps two cursors in Google's own clock, to the second, with
the ids that sit exactly on that second — so a second task in the same
second is neither lost nor shown twice:

- completions follow a task's `completed` time, asked for with
  `completedMin`, `showCompleted` and `showHidden` (Google's own apps
  hide a task when it is ticked off). Editing a task that was already
  completed does not report it again;
- changes follow its `updated` time, asked for with `updatedMin`.

Google Tasks cannot be asked for an order, so a check reads every page
since the cursor and sorts here; `more` is exact. A new watch starts
from the newest completion and change already in the lists, read once
when it is recorded. A check that finds nothing costs no model call.

## Dates

Google Tasks keeps a due date, not a time: due is `YYYY-MM-DD`, written
as midnight UTC. `completed` and `modified` are UTC moments.

## Limits, and what is not verified

- Live Google Tasks behaviour is **not yet verified**: the tests run the
  agent in a real worker against a loopback stub of the API subset it
  uses. In particular, `users/@me/lists/@default` resolving to the
  default list, whether `updatedMin` and `completedMin` are inclusive,
  clearing a due date or a completion time by sending `null`, and the
  `webViewLink` field follow the documentation but are untested against
  Google.
- Because Google cannot sort, `tasks.list` reads a whole list (up to a
  thousand tasks) to show the most recently changed first; its page
  token is a position in that order, so pass the same list and filters.
- Deleted tasks are not reported by `sync.changed`.
- Moving a task under another parent, or reordering, is not offered.
- Lists are capped at 25 per answer; tasks at 25 per page.
