# Spreadsheets (`sheets`)

A supplier price list arrives as Excel with duplicated rows and prices
typed as text. Ask what is wrong with it and the assistant names the
columns, the duplicates and the gaps; ask for a cleaned copy and a new
workbook comes back with a change log as its last sheet. The original is
never touched.

Reads CSV and Excel files, says what each column holds, finds
duplicates, gaps and inconsistent values, reconciles two lists by
explicit rules, aggregates with decimal arithmetic, and produces a new
workbook whose last sheet is the change log. The original file is
never modified, and no number is ever added up by a model.

No external service and no credential.

## Functions

| Function | Level | What it does |
|---|---|---|
| `read.inspect` | 0 | Sheets, row counts, and per column: guessed type, blanks, formulas, leading zeros |
| `read.rows` | 0 | Rows as objects keyed by column name, paged; values as text as they appear; formulas on request |
| `check.quality` | 1 | Duplicate groups by key columns, blank required cells, values differing only in case or spacing |
| `reconcile.match` | 2 | Two lists matched by explicit key rules; a workbook with Matched, Only in A, Only in B, Uncertain, Differences, Change Log |
| `clean.dedupe` | 2 | A workbook with Clean (first of each duplicate group kept), Removed (with original row numbers), Change Log |
| `combine.files` | 2 | Several files into one workbook: Summary (rows, totals, problems per file), All (every row with its source), a sheet per file, Change Log |
| `calc.aggregate` | 1 | Group and sum / count / avg / min / max with decimals; non-numeric cells counted, never guessed |

Records: `job` (every check, reconciliation, clean-up or aggregation
with its inputs, output and summary). Files: `source` in, `workbook`
out.

## What is preserved

- **Identifiers**: a value like `00123` is read as text and written as
  text; it never becomes 123.
- **Dates**: an Excel date cell stays a date cell.
- **Formulas**: read with both the formula and the file's cached
  value; written back as the formula.
- **Row numbers** in every finding are the sheet's own, so a person
  can open the original and look.

## Matching is a rule, not a guess

`reconcile.match` takes the rules: which column in A matches which in
B, and how both are normalized first — `trim`, `lowercase`, `digits`
(for phone numbers), or `none`. Rows equal on every key are matched.
Rows equal on every key but one, when exactly one such candidate
exists, are **uncertain**: written side by side on their own sheet and
listed in the result, never merged. Anything else is only in A or
only in B. Matched rows are diffed on the `compare` columns.

The same normalization drives `clean.dedupe` and `check.quality`.
Phone-like key columns in the quality check compare by digits only.

## Limits

- Up to 50,000 rows per sheet are read.
- CSV delimiters are sniffed (comma, semicolon, tab, pipe); a file
  that fools the sniffer reads as one column — inspect first.
- Cell formatting (colours, widths) is not carried into produced
  workbooks; values, dates and formulas are.
- Legacy `.xls` is not supported; save as `.xlsx` or CSV.
- Produced workbooks are verified by reading them back with openpyxl;
  rendered appearance in Excel is not inspected.
