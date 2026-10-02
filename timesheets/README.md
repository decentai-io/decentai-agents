# Timesheets (`timesheets`)

Friday afternoon, and the week has to be accounted for. Ask for this
week's meetings to become timesheet entries: the showroom meetings
with Harbourline land on the showroom project, the stand-ups on admin,
and the lunch no rule knows about comes back as a question rather than
a guess. Then ask where the gaps are — Wednesday, two hours — fill it,
submit the week, and get the workbook to send.

## Functions

| Function | Level | What it does |
|---|---|---|
| `projects.add` | 1 | A project, by short code; billable unless said otherwise, with an optional hour budget |
| `projects.list` | 0 | Projects with hours logged and budget left |
| `projects.update` | 1 | Rename, change billable or budget, archive |
| `projects.add_rule` | 1 | Place meetings on a project when the title, an attendee or the location contains a text |
| `projects.rules` | 0 | The rules |
| `projects.remove_rule` | 1 | Stops a rule; what it placed stays |
| `entries.log` | 1 | Hours on a project on a day — a number, or a start and end |
| `entries.from_events` | 1 | Calendar meetings into entries, by the rules; returns what it could not place |
| `entries.list` | 0 | A week's entries, or a date range's, with the total |
| `entries.update` | 1 | Changes an entry in an open week |
| `entries.remove` | 1 | Removes an entry from an open week |
| `weeks.summary` | 0 | The week by project and day, billable hours, and the gaps |
| `weeks.submit` | 1 | Hands the week in; its entries lock |
| `weeks.reopen` | 1 | Unlocks a submitted week |
| `weeks.workbook` | 2 | The week as an Excel workbook, read back before it is returned |

Records: `project`, `rule`, `entry` (hours on one project on one day)
and `week` (a week handed in, and when).

## From the calendar, by your rules

The calendar agents list meetings; this agent takes those rows as they
are. Each meeting's length is rounded to the quarter hour and logged
on the day it took place. Which project it goes on is decided by the
user's rules, in plain terms — the title contains "Harbourline", an
attendee's address contains "harbourline.example", the location
contains "Riverside". A meeting no rule places, or one that two rules
place on different projects (the "Harbourline stand-up"), comes back
unassigned with the reason; the user's answers go back in as
`assign`, an event id and a project code for each. A meeting already on the timesheet is not
logged again, and an all-day, cancelled or declined one is not time
spent.

## Rules it keeps

- **Nothing guesses a project.**
- **A day holds at most 24 hours.**
- **A gap is a weekday that has happened.** Days after `as_of` (today)
  are not gaps; `daily_hours` sets what a full day is (8).
- **A submitted week is locked.** Nothing is logged, changed or removed
  in it until it is reopened.
- **Hours add up.** They are kept to two decimals and summed as
  decimals, so the week's total is its days' totals.
- **Sending is someone else's job.** The workbook is a file; the mail
  agent sends it.

## Sample data

Lina's week of 7 September at Sidra Office Supplies: three projects
(the Harbourline showroom, billable with a 60-hour budget; the
Riverside office move; admin), the five rules that place her meetings,
and four days logged — Monday, Tuesday and Thursday full, Wednesday
two hours short of nothing, and Friday still to come.

## Limits

- One person's timesheet; there is no approver here, and rates and
  invoicing are out of scope.
- Weeks are ISO weeks, Monday to Sunday; business days are Monday to
  Friday.
- Meetings are taken as listed; the agent does not read calendars
  itself.
