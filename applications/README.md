# Applications (`applications`)

Ask for a travel authorization and the assistant asks you one thing at
a time: who is travelling, where, when, why, what it costs, then a copy
of the passport. Each answer is checked as you give it — a return date
before the departure is refused, a passport that expires too soon is
refused with the reason — and everything you have answered so far is
kept, so you can come back tomorrow and pick up where you left off.
Submitting is your own final step, and it waits for your word.

The pattern behind a guided intake: the **form owns the sequence**, the
**agent checks every step**, the **record is the truth**, and the model
only phrases questions and reads documents — it never decides what to
ask or whether an answer is acceptable.

## Functions

| Function | Level | What it does |
|---|---|---|
| `forms.list` | 0 | The forms this agent carries, with their steps |
| `applications.list` | 0 | The person's applications, with status and progress |
| `applications.start` | 1 | Starts an application on a form; returns the first step |
| `applications.next` | 0 | The next step to ask — field, label, kind, choices, required — and how many remain |
| `applications.answer` | 1 | Records one answer, checked against the form; refused answers come back with their problems |
| `applications.attach` | 1, model | Attaches a document to a document step: type, size and pages checked; what the form asks to read is read by the model and held to the form's rules |
| `applications.step` | 1, model | Asks the person the next step itself — a card with choices, or an attach button — then records the answer exactly as `answer` or `attach` would |
| `applications.review` | 0 | The whole application, what is missing, and whether it is ready |
| `applications.submit` | 3 | Closes a ready application and gives it a reference |
| `applications.withdraw` | 1 | Closes a draft without submitting |

Records: `application` (form, status, progress, reference, and the
answers and documents as values). Files: `document` (PDF, PNG, JPEG,
up to the platform's limit).

## Forms

A form is a YAML file in `forms/`. Its steps are asked in order. Each
step has a `field`, a `label` (the question), a `kind`, and the checks
its answer must pass:

| kind | checks |
|---|---|
| `text` | `min_length`, `max_length`, `pattern` with a `pattern_hint` for the refusal |
| `number` | `min`, `max`; stored to the cent |
| `date` | written as YYYY-MM-DD or refused; `not_before: today`; `after: <earlier date step>` |
| `choice` | one of `choices` (2 to 8); an optional choice step offers Skip |
| `email` | an address |
| `file` | `accept` (types), `max_mb`, `max_pages`; `read` names what the model reads from it; `checks` hold what was read to rules |

The one document check today is `date_after`: a date the model read
(`read_field`) must be at least `months` after an earlier date step
(`after_field`). A form that fails these rules is refused by name and
fault when the agent is used, not silently asked in a broken shape.

Two forms are carried as examples: a travel authorization with a
passport copy, and an equipment request. Add your own beside them.

## What is checked, and by whom

- **Answers** are checked in code. A date not written as a date is not
  a date; a number outside its bounds is refused; a choice must be one
  offered. Nothing is guessed.
- **Documents** are checked in code for type, size, pages and
  emptiness. Where the form says what to read, the chat's model reads
  it: a PDF by its text, a photo by its pixels (`call.llm(images=…)`).
  Each value read is marked **verified** when the document's text
  carries it, **assumed** when it does not (or the document is a
  photo), **missing** when the model found nothing. The form's rules
  are then applied to what was read, and a value that could not be read
  as a date fails the rule that needs it rather than passing by absence.
- **Submission** is the person's: level 3, waiting for approval unless
  the chat was trusted with it, and refused while anything required is
  missing.

## The reference pattern

`applications.step` is the whole intake in one function: it asks the
person the next question as a card of the right shape (choices as
buttons; an attach button for a document), records the answer, and
reports what came of it. One question per call, so a call is short and
a restart between questions loses nothing — the record already holds
every accepted answer. The assistant calls it until it says done, then
`review`, then `submit` on the person's word.

## Limits

- The forms are files in this agent; changing one is a new version.
- A photo is read by the model, not verified against text: everything
  read from a photo is assumed.
- Submission closes the record and gives it a reference; handing it to
  an external system is a further agent's job.
