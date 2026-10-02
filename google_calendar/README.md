# Google Calendar (`google_calendar`)

You need an hour with Dana next week, afternoons only. Ask for three
options and the assistant checks both calendars, says whose calendar it
could not see, and proposes slots that fit. Pick one, and booking it —
with the invitation — is the step you approve.

Lists Google Calendar events, checks who is free and says whose
calendar could not be checked, proposes meeting times deterministically
within stated constraints, and creates, updates or cancels events only
as level-3 actions on the user's explicit go-ahead.

Same Google connection as the Gmail agent: connect the account once,
grant it to both.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the connection and the calendar's time zone |
| `events.list` | 0 | Events in a window, recurring ones expanded, with links; `page_token` continues |
| `events.get` | 0 | One event with each attendee's actual response |
| `events.availability` | 0 | Busy periods for the account and named attendees; `unchecked` names calendars that could not be read |
| `events.find_times` | 1 | Candidate slots: business days, working hours, avoid windows (lunch), buffers around existing meetings, free for every checked calendar; each recorded as a proposal |
| `events.create` | 3 | Books a proposal or explicit times; invitations go out |
| `events.update` | 3 | Moves or retitles an event |
| `events.cancel` | 3 | Cancels an event; attendees are notified |

Records: `proposal` (each slot offered, and whether it was booked) and
`booking` (every event created, moved or cancelled, with Google's id).

## How a time is found

`find_times` is arithmetic, not a model's guess. For each business day
in the window it takes the working hours, removes the avoid windows,
removes every busy period of every checked calendar padded by the
buffer on both sides, removes anything before `not_before`, and walks
what is left in 15-minute steps, taking at most two slots per day so
the user sees a choice of days first. The same inputs always give the
same slots.

An attendee whose calendar Google will not show — not shared, or not
a Google account — is returned under `unchecked`, never treated as
free. Free/busy for another Gmail account works once that account
shares its calendar's free/busy with this one.

## What booking means

`events.create` returns Google's event id and link and records a
booking. **Invited is not accepted**: `events.get` shows each attendee's
real response. An unanswered create is reported as `kind: unknown` and
never retried here; the user should look at the calendar.

## Time zones and dates

Naive times are read in the calendar's own zone (or `timezone` when
given, an IANA name such as `Asia/Dubai`); times with an offset are
respected. Dates in `find_times` are `YYYY-MM-DD`. "Next week" and
"Thursday" are the assistant's to resolve and confirm before booking.

## Setup

The Gmail agent's README covers the Google Cloud project and the
organization's app registration. Connect the account from any Google
agent's Credentials tab — every Google agent asks for the same scopes —
and grant the same credential to this agent.

## Limits, and what is not verified

- Live Google Calendar behaviour is **not yet verified**: the tests run
  the agent in a real worker against a loopback stub of the API subset
  it uses.
- Only the account's primary calendar is read and written.
- Business days are Monday to Friday; no holiday calendar.
- `find_times` windows are capped at 31 days and 20 candidates.
- Attendee names are not resolved here; addresses come from the user
  or from a mail thread the mail agent read.
