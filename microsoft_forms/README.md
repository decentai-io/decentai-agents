# Microsoft Forms (`microsoft_forms`)

"Show me the latest answers to the customer feedback form, and tell me
whenever a new one comes in" — the responses are read with each answer
under its question, newest first, and the assistant is woken only when
someone has actually submitted the form.

## Read this first: what it can and cannot reach

**Microsoft Forms has no supported API.** Microsoft Graph offers no
endpoint for forms or their responses, and this agent calls no
undocumented Forms endpoint. It reads the one place Graph does serve
a form's responses from: **an Excel workbook in the person's OneDrive
or SharePoint that the responses sync to live.**

- A form **created from Excel for the web** (Insert → Forms), or **from
  OneDrive for Business** (New → Forms for Excel), keeps its responses
  in a workbook whose table (Microsoft names it `Form1`) gains a row
  per response. Those forms work here.
- A form created at forms.office.com has no such workbook. Its
  **"Open in Excel" download is a one-off copy**: it holds the responses
  of that moment and never updates, so a watch on it never fires.
- A form in a group or a SharePoint site keeps its workbook in that
  site's library, which this agent does not browse (see Limits).

The agent changes nothing, in Forms or in the workbook.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, and which address it is |
| `forms.find` | 0 | .xlsx workbooks in the drive, each opened to say whether it holds a response table — with the table, its questions and its response count; matches first |
| `responses.list` | 0 | The latest responses, newest first: ID, start and completion times, email, name, and answers keyed by question |
| `responses.watch` | 1 | Starts watching a response workbook for responses from now |
| `responses.new` | 1, schedulable | Responses submitted since the last check, oldest first, each naming its watch; moves the watch; `more` |

A table is taken to be a response table when its header carries the
columns Forms writes: `ID`, `Start time`, `Completion time`, `Email`
and `Name`. Every other column is a question, titled as the form asks
it — except `Last modified time`, which Forms also fills.

Records: `watch` — each watched response workbook, the highest
response ID handed on, and how many rows the table held then.

## Watching for responses

The assistant records one watch and schedules `responses.new` with
`wake_field: responses` every few minutes. A check that finds nothing
costs no model call; a check that finds responses wakes the assistant
with them, and the standing instruction in that chat says what to do
with each.

The cursor is the response **ID**, which Forms numbers in order, not
the row count: a person tidying the workbook deletes rows, and after
that a count points past rows nobody has seen. Each check reads from
25 rows before the remembered count and hands on the rows whose ID is
higher than the last one handed on, so a response is handed on once
however the rows around it moved. A row typed into the table by hand,
with no numeric ID, is never a response.

## Setup

The Microsoft connection is the one the other Microsoft agents use: an
app registration in Microsoft Entra with the delegated Graph
permissions listed in the manifest (`Files.ReadWrite.All` is the one
this agent needs), pasted into **Settings → Connected apps** as
provider `microsoft`. Then **Agents → Microsoft Forms → Credentials →
Connect account**, or grant an account already connected for another
Microsoft agent. The `api_base_url` field exists so the tests can point
the agent at a loopback stub; leave it empty.

For a form to be readable, create it from Excel for the web or from
OneDrive for Business so its responses sync to a workbook, and keep
that workbook in the same account's OneDrive.

## Limits, and what is not verified

- Only forms whose responses are kept in an Excel workbook in the
  account's own OneDrive. A personal form's "Open in Excel" download
  does not update; group and SharePoint-site workbooks are not
  addressed.
- Responses are rows as Excel holds them: if someone edits or deletes a
  row in the workbook, that is what is read. Deleting more than 25
  rows between two checks can hide the responses that arrived in
  between.
- At most 25 responses per call, 50 columns per response, each answer
  cut at 300 characters.
- The tests run the agent in a real worker against a loopback stub of
  the Graph workbook subset it uses (`tests/graph_workbook_stub.py`);
  the first real connection is the first live run. Not yet confirmed
  against a live response workbook:
  - the exact header text Forms writes today (`ID` vs `Id`, and whether
    quiz forms add `Total points` or feedback columns, which would be
    read as questions) — matching is case-insensitive;
  - how soon a new response appears as a row after it is submitted
    (Microsoft describes the sync as live, but not its delay);
  - that Excel keeps one blank body row in a response table with no
    responses, counted here as zero;
  - that `$select=text` is honoured on
    `worksheets/{name}/range(address='…')`, so start and completion
    times come back as Excel displays them rather than as date serials.
