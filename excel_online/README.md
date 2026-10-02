# Excel Online (`excel_online`)

"What came into the Orders table this week, and add Harbourline's
twenty desks to it" — the workbook is found in OneDrive, the table read
by its headers, and the new row waits for your go-ahead before it is
written. Ask to be told when an order is added, and the assistant is
woken only when one was.

Excel workbooks (.xlsx) in one Microsoft 365 OneDrive, read and written
where they are, over Microsoft Graph's workbook API. Nothing is
downloaded: only the cells asked for cross into the platform. It rides
the same Microsoft connection as the other Microsoft agents: connect
once, grant the one saved account to each.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | The connection, and which address it is |
| `workbooks.find` | 0 | .xlsx workbooks matching a query (every workbook when none is given) |
| `workbooks.get` | 0 | A workbook's worksheets (used range, rows, columns) and tables (worksheet, header row, row count) |
| `range.read` | 0 | A block by A1 address, or the used range, as rows of text; `header` takes the first row as the header; says how many rows and columns were cut |
| `table.rows` | 0 | A table's rows as objects keyed by header, a page at a time |
| `table.add_rows` | 3 | Appends rows given as objects keyed by header, in one workbook session; recorded |
| `range.update` | 3 | Overwrites a block, in one workbook session; recorded with what the cells held before |
| `rows.watch` | 1 | Starts watching a table (preferred) or a worksheet for rows added from now |
| `rows.new` | 1, schedulable | Rows added since the last check, keyed by header, each naming its watch; moves the watch; `more`; `reset` |

Records: `write` — every append and overwrite this agent made, with
what it wrote and, for an overwrite, the previous cells as formulas
(what would put them back); a write whose answer never came is
`unknown`. `watch` — each watched table or worksheet and how many data
rows it held at the last check.

## Watching for new rows

The assistant records one watch and schedules `rows.new` with
`wake_field: rows` every few minutes. A check that finds nothing costs
no model call; a check that finds rows wakes the assistant with them,
and the standing instruction in that chat says what to do with each.

The watch is a row count: rows past it are new, because a table grows
at the end. When the count has shrunk — rows deleted, or a sort pasted
over — the rows past the old count cannot be told apart from rows that
moved, so nothing is reported for that watch that time, the count
starts again from the table's current size, and `reset` names the
watch with a sentence the assistant can pass on. A table's rows are
preferred to a worksheet's used range: a table knows its own header
and end, while a used range also grows when someone types a note two
rows below the data.

## Rules it keeps

- **Nothing in a workbook changes without a yes.** Appending and
  overwriting are level 3.
- **A column is a header, not a letter.** Rows to append are keyed by
  header; a name the table lacks is refused, never dropped.
- **An overwrite keeps the old cells.** The shape must match the
  address exactly, and the previous content is recorded and returned
  before anyone has to ask.
- **Reads are capped.** At most 25 rows per read and 50 columns, each
  cell's text cut at 300 characters, with the count of what was cut.
- **Cells are read as text** — the date and the amount as Excel shows
  them — rather than as serial numbers and bare floats.

## Setup

The Microsoft connection is the one the other Microsoft agents use: an
app registration in Microsoft Entra with the delegated Graph
permissions listed in the manifest (`Files.ReadWrite.All` is the one
this agent needs), pasted into **Settings → Connected apps** as
provider `microsoft`. Then **Agents → Excel Online → Credentials →
Connect account**, or grant an account already connected for another
Microsoft agent. The `api_base_url` field exists so the tests can point
the agent at a loopback stub; leave it empty.

## Limits, and what is not verified

- Only .xlsx workbooks in the account's own OneDrive. Workbooks in
  SharePoint libraries or shared from someone else's drive are not
  addressed; .xls, .xlsm and .csv are not opened by the workbook API.
- A row watch sees rows added at the end. Rows inserted in the middle
  or edited in place are not new rows.
- The tests run the agent in a real worker against a loopback stub of
  the Graph subset it uses (`tests/graph_workbook_stub.py`); the first
  real connection is the first live run. Details taken from Microsoft's
  documentation and not yet confirmed live:
  - that `$select=text` / `$select=formulas` is honoured on
    `worksheets/{name}/range(address='…')`, and `$select=address` on a
    table's `range`;
  - that `usedRange(valuesOnly=true)` answers "A1" for an empty sheet;
  - that a table with no data rows reports one blank body row, which
    is counted here as zero rows;
  - that `createSession` with `persistChanges: true`, the
    `workbook-session-id` header on `rows/add` and on a range `PATCH`,
    and `closeSession` behave as documented on a personal (consumer)
    OneDrive as well as a business one;
  - that drive search with the query `xlsx` finds workbooks by
    extension when no query is given.
