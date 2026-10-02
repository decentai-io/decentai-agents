# Microsoft Calendar (`microsoft_calendar`)

Dana asks for a call next week. Ask for three afternoon slots when you
are both free: the answer is computed from both calendars, lunch kept
clear, and it says plainly whose calendar it could not read rather than
calling them free. Pick one and book it — and nothing is sent until you
say so.

Google Calendar's twin, over Microsoft Graph: the same functions, the
same records and the same rules, for a Microsoft 365 calendar. Lists
events, reports busy periods and whose availability could not be
checked, finds candidate times deterministically, and creates, moves or
cancels events only as level-3 actions.

It uses the same Microsoft connection as every other Microsoft agent.
Connect once, grant the one saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, and the calendar's time zone as an IANA zone |
| `events.list` | 0 | Events in a window, recurring ones expanded, in the calendar's zone |
| `events.get` | 0 | One event with each attendee's actual response |
| `events.availability` | 0 | Busy periods for the account and attendees; who could not be checked |
| `events.find_times` | 1 | Candidate slots from both calendars, working hours, avoid-windows and buffers; each recorded as a proposal |
| `events.create` | 3 | Books a meeting from a proposal or explicit times; invitations go out |
| `events.update` | 3 | Moves or retitles an event |
| `events.cancel` | 3 | Cancels an event; its attendees are told |

Records: `proposal` (a slot offered, and whether it was taken) and
`booking` (every event this agent created, moved or cancelled).

## Time zones

A Microsoft 365 mailbox usually reports its zone as a Windows name —
"Arabian Standard Time", "GMT Standard Time". The agent asks Graph for
UTC on every call and does its own arithmetic, translating the mailbox's
zone once: the common Windows names map to their IANA zones
(Asia/Dubai, Europe/London), an IANA name passes straight through, and a
zone it cannot map is reported as a problem and the agent asks for one,
rather than reckoning in the wrong zone. Any call can take `timezone` to
override the mailbox's.

## Finding a time is arithmetic

`find_times` takes the busy periods of every calendar that could be
read, business days, working hours, avoid-windows and a buffer around
existing meetings, and returns slots that fit what is left, at quarter
hours, at most two a day so the user sees a choice of days first. It
asks no model; the same inputs always give the same slots. An attendee
outside the organization, or whose calendar is not shared, is named in
`unchecked` and is never counted as free.

## Limits

- Only the account's default calendar is read and written.
- Business days are Monday to Friday; no holiday calendar.
- `find_times` windows are capped at 31 days and 20 candidates.
- Attendee names are not resolved here; addresses come from the user or
  from a mail thread the mail agent read.
- Live Microsoft 365 behaviour is verified against a loopback stub of
  the Graph subset it uses; the first real connection is the first live
  run.
