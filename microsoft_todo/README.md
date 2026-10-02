# Microsoft To Do (`microsoft_todo`)

You tick tasks off on your phone in Microsoft To Do, and keep the rest
of your work in Tasks. Ask for what is open and what is due this week;
add a task to To Do from the chat; and have the assistant check each
morning what you completed in To Do and mark the matching Tasks items
done — it wakes only when something was completed.

Google Tasks' twin, over Microsoft Graph: the same functions, the same
record and the same rules, for a person's own Microsoft To Do lists.
Reads lists and tasks; creates, edits, completes and reopens a task as
an ordinary change; deletes one only as a level-3 action.

It uses the same Microsoft connection as Outlook, Microsoft Calendar,
OneDrive and Teams. Connect once, grant the one saved account to all of
them. The connection asks for `Tasks.ReadWrite`.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection and which address it is |
| `lists.list` | 0 | The account's lists with their ids; `kind` names the default list and Outlook's flagged mail |
| `lists.create` | 1 | Makes a new list |
| `tasks.list` | 0 | A list's tasks (the default list when none is named), open / completed / all, due before a date, most recently changed first; `page_token` continues |
| `tasks.get` | 0 | One task whole: its note and its checklist items |
| `tasks.create` | 1 | Adds a task: title, due date, importance, note, reminder |
| `tasks.update` | 1 | Changes title, due date, importance, note or reminder; an empty due or reminder clears it |
| `tasks.complete` | 1 | Ticks a task off |
| `tasks.reopen` | 1 | Marks a completed task not started again |
| `tasks.delete` | 3 | Deletes a task; waits for the user's go-ahead |
| `sync.watch` | 1 | Starts watching one list, or every list, from now (or from `since`) |
| `sync.completed` | 1, schedulable | Tasks completed since the last check, oldest first, each with its list name and watch; moves the completion cursor; `more` says a later check continues |
| `sync.changed` | 1, schedulable | Tasks created or changed since the last check, completions included, each with its status; moves the change cursor |

Records: `watch` (how far the lists have been read, for completions and
for changes). The tasks themselves live in To Do, not on the platform.

## Why a personal task is level 1

A task in the person's own To Do is private and inert until they act on
it, like a mail draft: creating, editing, completing and reopening one
changes nothing anybody else sees. Deleting one cannot be taken back,
so it is level 3 and pauses for approval in a chat below that trust.

## Keeping in step with Tasks

Agents cannot call each other; the assistant carries a task across,
and the manifest tells it how.

- **To Do into Tasks**: `tasks.get` here, then the Tasks agent's
  `tasks.create` with `source` exactly `microsoft_todo:<task_id>`.
- **Tasks into To Do**: `tasks.create` here, with the note ending
  `Tasks item: <task_ref>`.
- **What was ticked off in To Do**: one `sync.watch`, and a schedule on
  `sync.completed` with `wake_field: completed`. Each row carries the
  `task_id` and the note, which is what finds the Tasks item — by
  `source`, or by the `task_ref` in the note — and the assistant marks
  it done with the completion date as evidence.

## Watching

The watch is a cursor in Graph's own clock: the `lastModifiedDateTime`
of the newest task handed on, to the second, plus the ids that sit
exactly on that second — so a second task completed in the same second
is neither lost nor shown twice. Completions and changes each keep
their own cursor on the one record, so scheduling both is safe. A watch
on every list reads each list since the cursor and merges them into one
order. Each check fetches one more than asked for, so `more` is honest.
A check that finds nothing returns an empty list and costs no model
call.

## Dates

Due dates are `YYYY-MM-DD`, written as midnight UTC so they read back
as the same day. A reminder is an ISO 8601 date-time with an offset or
`Z`; a time without one is refused rather than guessed. A reminder set
in the To Do app in another zone is shown with that zone, not
converted.

## Limits, and what is not verified

- Live Microsoft Graph behaviour is **not yet verified**: the tests run
  the agent in a real worker against a loopback stub of the To Do
  subset it uses. In particular, `$filter` on `lastModifiedDateTime`
  combined with `status`, `$orderby=lastModifiedDateTime`, the filter
  on `dueDateTime/dateTime`, and clearing a due date by sending `null`
  follow the documentation but are untested against Microsoft.
- Graph keeps `completedDateTime` as a day, not a moment, so the
  completion cursor follows `lastModifiedDateTime`: a completed task
  edited again in To Do is reported by `sync.completed` a second time.
- A new watch starts from the ten most recently changed tasks of each
  list; more than ten changed in that very second would be reported
  once as news.
- Checklist items are read, not written. Attachments, linked resources
  and categories are not read.
- Lists are capped at 25 per answer; tasks at 25 per page.
