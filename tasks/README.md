# Tasks (`tasks`)

A meeting ends with six promises and no list. The office lease renews for
another three years unless someone gives notice ninety days before. Both
are the same question — what must I do, and by when — and this agent
keeps both.

Paste the notes: the assistant reads out the action items with the
passage that states each, you confirm them, and they become tasks with
owners and dates. Record a subscription, a lease, an insurance policy or
a passport once, with the clause it came from, and ask each month what
falls due: the answer names the day you must act — the notice deadline,
not the renewal date — and whose decision it is.

No external service. The model is used only to read notes and documents;
every quote it returns is checked verbatim against the text. All date
arithmetic is code.

## Functions

**Tasks** — obligations, and the action items read out of notes.

| Function | Level | What it does |
|---|---|---|
| `tasks.create` | 1 | A task, with owner and due only as given |
| `tasks.update` | 1 | Status, owner, due, note; done needs evidence, blocked needs a blocker |
| `tasks.list` | 0 | By owner, status, source; due date first |
| `tasks.overdue` | 0, schedulable | As of a date: overdue, due soon, blocked, unassigned, undated |
| `extract.propose` | 1, uses the chat's model | Proposals from notes with verified quotes, commitment or suggestion, owner and date only when the notes give them |
| `extract.confirm` | 1 | Proposals the user agreed to become tasks; owner and due may be supplied now |
| `extract.reject` | 1 | A proposal marked not wanted |

**Renewals** — anything that renews or expires: a subscription, a
contract, an insurance policy, a warranty, a maintenance plan, a lease,
a licence, a passport or visa.

| Function | Level | What it does |
|---|---|---|
| `agreements.extract` | 0 (model) | Reads a document's text; proposes the counterparty, dates, term, notice period and auto-renewal with verbatim clauses; names ambiguities and asks; records nothing |
| `agreements.create` | 1 | Records it with its clauses; derives the renewal and notice deadlines; says what is still to ask |
| `agreements.get` | 0 | One renewal with its clauses, deadlines and decisions |
| `agreements.update` | 1 | Changes dates, notice period or basis, owner; recomputes and moves the open deadlines |
| `agreements.set_status` | 1 | renewed / cancelled (evidence and a name required) / expired |
| `deadlines.upcoming` | 0, schedulable | What is due or passed within a window, what awaits a decision, what cannot be computed; wake field `due` |
| `deadlines.add` | 1 | A further deadline: warranty end, expiry, review, custom |
| `deadlines.complete` | 1 | Records a deadline acted on, with evidence; never cancels anything |
| `decisions.record` | 1 | The intended decision (renew, cancel, renegotiate), by whom and when |

Records: `task` and `agreement` (both editable on the page), `extraction`
(proposals, with what became of each), `clause`, `deadline`, `decision`.

## Proposals are not tasks

`extract.propose` asks the chat's model for the action items and, for
each, the exact passage that states it. Then the code checks:

- the quote must appear verbatim in the notes, or the proposal is marked
  `verified: false`;
- the owner must be a name the notes (or the `people` list) actually
  contain, or it is cleared — an owner is never invented;
- the due date is accepted only when unambiguous: ISO, a written date
  with a month name, or a weekday name resolved from `meeting_date`.
  "Next week" stays as `due_text` for a person to decide.

A commitment is something a named person said they will do; a suggestion
is something someone said should be done. Both are proposals until
`extract.confirm`, where the user can add the owner or date the notes
lacked. A task confirmed without an owner is listed as unassigned until
someone takes it.

## Two dates, not one

Something that renews on 1 December with 60 days' notice has a renewal
deadline on 1 December and a notice deadline on 2 October (calendar
days) or 8 September (business days, Monday to Friday). The agent
computes the notice deadline only when it has all three: the exact
renewal date, the notice period in days, and the basis. While any is
missing it records what it has and returns `asks` naming what to get
from the user or the document. It never picks a basis, never completes
"December" into a day, and never turns "two months" into a number of
days.

`agreements.extract` asks the model for the dates and terms as written
and the clauses they come from. Code then checks each quote against the
text (`verified`), parses each date (only an exact day, month and year
counts), reads the notice period ("60 days", "60 business days", "sixty
(60) calendar days") and marks a clause `ambiguous` when the notice
period has no basis or the renewal date has no day. Nothing is recorded
until `agreements.create`.

## What needs me

Two deterministic checks, both fit for a clock. Neither asks a model,
and neither wakes anybody when there is nothing to say.

- `tasks.overdue` with wake field `overdue`: what is late, what is due
  within `within_days`, what is blocked, and what has no owner or no date.
- `deadlines.upcoming` with wake field `due`: every open deadline inside
  the window or already passed, with calendar and business days left;
  `awaiting_decision` for renewals whose notice deadline is near with no
  decision recorded; `incomplete` for those whose deadline could not be
  computed, and why.

## What is and is not a cancellation

- A reminder is a reminder.
- `deadlines.complete` on the notice deadline records that notice was
  given, with the message reference; the renewal stays active.
- `decisions.record` records an intention.
- Only `agreements.set_status` with the confirmation reference and a name
  marks something renewed or cancelled. Renewed takes the new renewal
  date and derives the next notice deadline.

## Limits

- Dates are calendar dates; there are no times of day.
- Business days are Monday to Friday; public holidays are not known.
- Notice periods in months are reported as ambiguous, not converted.
- Extraction quality depends on the chat's model; the checks catch a
  misquote, an invented name and an ambiguous date, not a missed item.
- Documents are read from supplied text (use the Documents agent for PDF
  and Word); scanned pages without a text layer cannot be read.
- Values are stored as given; nothing is converted between currencies.
