# Notebook — the reference agent

A meeting ends and three decisions were made. Say so, and they are saved
in the "meetings" notebook; next month, ask what was decided about the
office move and the note comes back, with the date. Notebook is also the
reference agent: it exercises every feature DecentAI can enforce on a
domain small enough that the rules are the only thing to notice.

One agent that exercises every feature DecentAI can enforce, on a domain
small enough that the platform's rules are the only thing to notice.
Read `manifest.yaml` top to bottom to learn what an agent can declare;
read the tools to see how little code each feature needs.

The platform ships with no agents. Installing this one — from this
repository, pinned to a commit an administrator reviewed — is how a
deployment gets its first.

## What each function is here to demonstrate

| Function | Level | The feature |
|---|---|---|
| `note.save` | 1 | A contained write: `create`/`update` on a data resource, a scope (`notebook`) a policy can narrow, an `x-resource` reference in and out. Uses `titlecase` — a declared dependency, installed into the agent's own environment. |
| `note.get` | 0 | A read by reference; optional output fields travel only when present, because the output schema is enforced. |
| `note.find` | 0 | **`schedulable`**: the clock may run it unattended, with no model and no approval, and wake the assistant only when `notes` comes back non-empty. Uses `humanize`. |
| `note.summarize` | 1 | **`llm: true`**: the one way agent code thinks — `call.llm` reaches the chat's configured model through the platform; the stored key never enters the agent's process. |
| `archive.export` | 2 | Files: `create` a document under declared constraints (MIME types, size). |
| `archive.import` | 2 | Files: `read` a document a person may have uploaded themselves (`user_access: [create]`), validated whole before any note is written. |
| `sync.status` | 0 | Secrets: `use` a bound connection — `keys` readable, `values` encrypted and decrypted only for the function that declared it; audited by name, never by value. |
| `sync.push` | 3 | External and irreversible: in an ordinary chat this asks the person for approval, and the runtime re-verifies the exact inputs before it runs. |

Also on show: two real pip dependencies (the platform builds this agent a
private environment and installs exactly those), JSON Schema on every
input and output, and the agent's own `instructions` — which the model
reads only after opening the agent.

## The remote is simulated

`sync.*` consume a real bound secret but never make a network request:
the digest they return is computed from the notes and the token. This
keeps the reference safe and deterministic while still exercising the
whole secret path.

## Copy it to create an agent

1. Copy this directory; give the copy a lowercase, stable id.
2. Make the directory name, `agent.id` and the catalog entry that same id.
3. Rename `NotebookAgent` and update `implementation.entrypoint`.
4. Keep only the resources your agent genuinely needs, and the narrowest
   operations and permission level for every function.
5. Write `instructions` for the model: when to use the tools, what never
   to assume.
6. Add tests (see `tests/`) and run them before publishing.
