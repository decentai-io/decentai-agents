# DecentAI Agents

General-purpose agents for DecentAI: the accounts you already work in,
the files you hand over, and the things you keep. Each one is attached to
something a person already has, so it answers a question on the first
afternoon, and each is written the way the platform wants agents written.

DecentAI ships with no agents. A deployment adds this repository as an
agent source, an administrator reviews each manifest, and approval pins
that agent to the exact commit reviewed. Reading a source never executes
its code.

## The catalog

**Accounts you connect** — sign in once; the platform keeps the tokens,
refreshes them, and hands them to the agent when it calls the provider.
Every Google agent asks for the same permissions, and so does every
Microsoft agent, so one connected account can be granted to all of them.

*Mail, calendars and chat*

| | |
|---|---|
| [`gmail/`](gmail/) | Find, read, draft and, on your word, send. Reminders that fire only while a reply is missing, and an inbox watched so a schedule wakes only for new mail |
| [`outlook/`](outlook/) | The same for a Microsoft 365 mailbox, through Microsoft Graph |
| [`google_calendar/`](google_calendar/) | What is on, who is free, and a booking that waits for your go-ahead |
| [`microsoft_calendar/`](microsoft_calendar/) | The same for a Microsoft 365 calendar |
| [`teams/`](teams/) | Catch up on a chat, see whether someone answered; send or post on your go-ahead |
| [`slack/`](slack/) | The same for Slack, as you, with a watch that wakes only for a mention or a direct message |
| [`mail/`](mail/) | Any mail account, connected with its address and an app password and nothing registered with a provider: find, read a conversation, save an attachment, draft, send on approval, and the same two watches. Over IMAP and SMTP, through the platform's proxy |

*Files and storage*

| | |
|---|---|
| [`google_drive/`](google_drive/) | Find a file, see who can open it, bring a copy in; file and share on your go-ahead |
| [`onedrive/`](onedrive/) | The same for a Microsoft 365 OneDrive |
| [`dropbox/`](dropbox/) | The same for a Dropbox, and a folder watched so a schedule hears only what changed |
| [`box/`](box/) | The same for a Box account, with its change stream watched the same way |

*Live documents, spreadsheets and forms*

| | |
|---|---|
| [`google_sheets/`](google_sheets/) | A live Google spreadsheet read, written on your go-ahead, and watched for new rows |
| [`excel_online/`](excel_online/) | The same for an Excel workbook in OneDrive, each write one workbook session |
| [`google_docs/`](google_docs/) | A Google Doc read by paragraph, commented on, and edited only as a proposal you apply — refused if the doc changed since |
| [`word_online/`](word_online/) | The same for a Word document in OneDrive, read from the file itself, with a watch for saves and new comments |
| [`google_forms/`](google_forms/) | What a Google Form asks, its responses, and a watch that wakes you only when someone answered |
| [`microsoft_forms/`](microsoft_forms/) | A Microsoft Form's responses, read from the Excel workbook they sync to — Microsoft offers no Forms API |
| [`notion/`](notion/) | Pages and databases the person shared: read, a row written against the database's own schema, and changes watched |

*Tasks elsewhere*

| | |
|---|---|
| [`microsoft_todo/`](microsoft_todo/) | Your To Do lists: what is open and due, tasks added and ticked off, and what you completed there brought back to Tasks |
| [`google_tasks/`](google_tasks/) | The same for Google Tasks |
| [`todoist/`](todoist/) | Your Todoist: what is due, a task filed where you said, and what you ticked off carried into your other lists |

**Services on the organization's key** — an administrator pastes the key once.

| | |
|---|---|
| [`places/`](places/) | Find a place, read its hours and phone, and see how far and how long a trip is — from Google Maps |

**Files you hand over** — read with page references and quoted evidence.

| | |
|---|---|
| [`documents/`](documents/) | PDFs, Word files and text read with page references; fields extracted with the sentence behind each; Word and PDF produced |
| [`sheets/`](sheets/) | Inspect, reconcile and clean CSV and Excel files, with a log of every change |
| [`slides/`](slides/) | PowerPoint decks read slide by slide with their speaker notes; new decks produced from an outline, in your own template |
| [`json_data/`](json_data/) | JSON and JSON Lines: what is actually in a file, values by path, a schema checked, two versions compared, a CSV out |
| [`expenses/`](expenses/) | Receipts into an itemized total you can defend: duplicates caught in code, your policy checked if you have one |

**The public web** — only public addresses are ever fetched; nothing inside the network the platform runs in.

| | |
|---|---|
| [`web_reader/`](web_reader/) | A web page or online PDF read with the Documents rules: quoted, with its section or page, never invented; saved for another agent to read |
| [`web_watch/`](web_watch/) | A page watched for the change that matters — a price, a phrase, any edit — reported once, on a schedule |
| [`feeds/`](feeds/) | RSS and Atom subscriptions, found from a site's address; what was published since the last look, exactly once |

**Work nothing else covers** — each step is yours to see, and to stop.

| | |
|---|---|
| [`browser/`](browser/) | A real web browser, driven by the chat's model, on any site: find things, fill forms, read what a page says. You watch it in the chat and take it over when a site wants a person; sign-ins are asked for on a card and kept as your secrets; paying, sending, deleting or publishing stops for your approval |
| [`code_runner/`](code_runner/) | A Python program written for what you ask and run once you allow it: you see the program first, with the packages it installs, the sites it reaches, the credentials it uses and the files it reads, and it gets those and nothing else |

**Checking the platform**

| | |
|---|---|
| [`connection_check/`](connection_check/) | What an agent can reach from where it runs: the one host it declared, one it did not, an address inside the network and a way around the platform, each tried and told apart as reached, refused or unreachable |

**What you keep**

| | |
|---|---|
| [`tasks/`](tasks/) | What you have to do, and everything that renews or expires — the notice deadline, not the renewal date |
| [`timesheets/`](timesheets/) | Where the week went, by project — meetings placed by your own rules, locked when handed in |
| [`notebook/`](notebook/) | Notes and decisions, kept and findable — and every feature the platform enforces, in one agent |
| [`applications/`](applications/) | A guided application on a form the agent carries: one question at a time, every answer and document checked as it arrives, submitted on your word |

Beside them: [`decentai-agents.yaml`](decentai-agents.yaml), the catalog
DecentAI reads, and [`tests/`](tests/), where every agent runs in a real
worker over the real wire against the platform's simulated resources.

## Install it

1. In DecentAI, open **Agents → Marketplace** and add this repository as
   a source.
2. Read the manifest the platform fetched — dependencies, tools,
   permissions, scopes, secrets, data, files.
3. Approve. The runtime pulls exactly the approved bytes, by digest,
   when a chat first needs them.

## Build your own

Start from the Note example in the platform's repository,
[`examples/note/`](https://github.com/decentai-io/decentai/tree/main/examples/note),
which is one agent that uses every feature the platform can enforce,
and the walkthrough for copying it in
[`docs/agents/`](https://github.com/decentai-io/decentai/tree/main/docs/agents):
the anatomy of an agent, every manifest key and everything the SDK
gives, how to try one on your own computer, publishing and versions,
and the checklist to run before you ask anyone to approve it.
`notebook/` here is the same
agent at full size; the rest of this catalog is what these ideas look
like on real work.

**Every change to a manifest is a new version.** An installation
approves a manifest by its `agent.version`, and a version is immutable
once approved: the platform refuses to re-approve `0.1.0` with
different content, even when only a sentence changed. Bump the
version with the change, or nobody can take the update.

## Sample data

An agent that keeps records may ship `samples.yaml` beside its
manifest: the records and files that let a person try it before they
have any of their own. The platform lists what a sheet would load, and
the agent's page loads it as the person's own records and files — and
removes it again — in one click. Every sheet in this catalog tells the
same story, **Sidra Office Supplies**, so the lease Tasks records is the
lease Documents ships to read, and an example prompt finds the records
it names.

```yaml
story: One line saying whose data this is.
records:
  - ref: lease
    slot: agreement            # a record type the manifest declares
    fields: {name: Office lease, counterparty: Riverside Estates, kind: lease,
             notice_basis: calendar, auto_renews: "yes", status: active, decision: undecided}
  - slot: clause
    fields: {agreement_ref: "@lease", topic: notice, ambiguous: "no", verified: "yes",
             reference: "Clause 14.2, page 6",
             quote: "Notice of non-renewal must be received by the Landlord not less than ninety (90) days before the Renewal Date."}
```

Files work the same way under `files:`, each with a `ref`, the file slot
the manifest declares, and a path inside the agent's folder.

A string field whose whole value is `@ref` becomes the id the platform
gave that row or file when it was loaded, so rows may point at each
other; a ref must be declared before it is used, in the same sheet.
Rows are checked against the manifest here (`tests/test_samples.py`)
and again by the platform at load time, like a person's own record.
The two PDFs under `documents/samples/` are made by
`tests/make_sample_pdfs.py`, and the deck under `slides/samples/` by
`tests/make_sample_decks.py`; both are committed.

## Run the tests

The tests run the agent the way production does — in its own worker
process, over the worker protocol — against DecentAI's simulated
resources, so they need a checkout of [the platform](https://github.com/decentai-io/decentai) beside this one and
its code on the path. Python 3.11 or later:

```bash
python -m pip install -r tests/requirements.txt
PYTHONPATH=../decentai python -m pytest tests -q
```

On Windows PowerShell, `$env:PYTHONPATH = "..\decentai"` first. Every
provider is a stub under `tests/`, served on this machine.

The first run builds the agent's environment under `tests/.workerenv`
(cached; delete it after changing dependencies).
