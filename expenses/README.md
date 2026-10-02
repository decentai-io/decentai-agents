# Expenses (`expenses`)

A week of travel leaves eleven receipts as PDFs. Upload them and ask for
a claim: each line shows the receipt text it came from, the two taxi
receipts that are the same ride are flagged, and the hotel line that
breaks the policy says which rule. Submit it, and the approver's
decision is recorded under their name.

Receipts into an expense claim, with every extracted fact checked
against the receipt's own text, assumptions kept apart from facts,
duplicates matched in code, the company policy as rules with quoted
sources, and a claim status that becomes approved or paid only on a
named reviewer's word.

No external service and no credential.

## Functions

| Function | Level | What it does |
|---|---|---|
| `claims.list` | 0 | The person's claims with their refs — how an existing claim is found |
| `claims.create` | 1 | A draft claim for a claimant, USD unless said otherwise |
| `claims.get` | 0 | The claim, its receipts, totals by currency, how many need attention |
| `claims.check` | 1 | Duplicates, receipts needing review or entry, other currencies, policy exceptions |
| `claims.workbook` | 2 | Receipts, Summary, Exceptions, Rules applied — read back |
| `claims.set_status` | 1 | draft → submitted on the claimant's word; approved, rejected, paid only with the reviewer's name |
| `receipts.read` | 1, uses the chat's model | One receipt file into the claim: merchant, date, currency, amount, category — verified or assumed |
| `receipts.confirm` | 1 | A person confirms or corrects a receipt; required for photos and anything needing review |
| `policies.load` | 1, uses the chat's model | The known rules out of the policy document, each with its sentence, checked verbatim |
| `policies.set_rule` | 1 | One rule set or corrected by a person |
| `policies.get` | 0 | The rules in force |

Records: `claim`, `receipt`, `rule` (rules are editable on the page).
Files: `receipt_file`, `policy_file`, `workbook`.

## Facts and assumptions

`receipts.read` asks the chat's model for the fields and then checks:

- the **amount** must appear on the receipt as a money figure, or it
  is assumed; if a larger figure is printed, the note says so;
- the **date** must parse — ISO, written with a month name, or
  day/month/year when the order is unambiguous — or it is assumed;
- the **currency** must be shown on the receipt by code or symbol, or
  it is assumed; a receipt showing exactly one currency is taken as
  that currency;
- the **merchant** must appear in the text, or it is assumed;
- the **category** is always the model's judgement and is listed as
  assumed.

A receipt with any assumed money fact is `needs_review` and cannot be
in a submitted claim until a person confirms it. A photo (jpeg, png)
or a scanned PDF has no text this agent can read: it is recorded as
`manual` with the file kept, for `receipts.confirm` to fill.

Another currency is claimed as it is and flagged; nothing is
converted.

## Duplicates and policy

A duplicate is the same merchant, date and amount already recorded —
in this claim or any other. It is flagged on read and again by
`claims.check`, which marks it `duplicate`.

`policies.load` reads six rule names out of the policy document —
meal allowance per day, hotel per night, taxi per trip, receipt
required above, claim window in days, alcohol allowed — each with the
sentence that states it, verified against the text. A rule the policy
does not state is reported as not found and never assumed; a person
can set it with `policies.set_rule`. `claims.check` applies the rules
in force and names each exception with its figures.

## Status

Preparing a claim proves nothing about reimbursement. `set_status`
lets the claimant submit a claim whose receipts are all extracted or
confirmed; approved, rejected and paid require the reviewer's name
and are stamped with the date. The agent never reports a claim as
approved or paid on its own account.

## Limits

- No OCR and no image reading; photos are manual entry.
- Dates with ambiguous day/month order (03/04/2026) are not read.
- Meal allowance is checked per calendar day in the claim's currency;
  other currencies are flagged, not summed against the allowance.
- Rules are one value each; tiered or per-city allowances are not
  modelled.
