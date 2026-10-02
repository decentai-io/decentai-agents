# Code (`code_runner`)

Someone has a spreadsheet of orders and wants the total by month and a
chart of it. No agent does exactly that, and they do not write code.
They ask; a card shows them the program that would do it and what it
needs; they press Allow.

This agent writes a Python program for a goal and runs it when the
person allows it. It is for the work no other agent does: a calculation
over files, data reshaped or merged, a chart, a file converted, a web
API that has no agent of its own.

## What the person sees

Every program goes to them on the platform's code card before it runs:

| On the card | What it is |
|---|---|
| Purpose | What the program does, in words |
| Installs | The packages it needs |
| Reaches | The sites it connects to |
| Uses | The credentials it is handed, each with the site it is for |
| Reads | The files it was given |
| The assistant's reading | Whether the program does what its purpose says, and what it does beyond that |
| The code | The whole program |

What runs is what they allowed. A program that fails is corrected and
goes before them again — a changed program is a new program, and its
card says it is a correction — up to three programs in one call.

In a chat that does not yet trust an agent to make files, the
platform's own approval of the call comes before it, as for any
function that makes files.

How often the code card is shown is the person's to set, on the
platform's **Settings → Safety** page: every time, which is where it
starts; not again for a correction that needs nothing new; or only
when a program reaches a site or uses a credential. A program that
runs without a card is one the assistant read and found to do what it
says, and the chat is told what ran, with the code. The same page holds
the sites no agent may open and the packages a program may install.

## What the program gets

What the card named, and nothing else:

- **Sites.** The agent itself declares no host. The platform opens the
  sites on an allowed card for that one run and closes them when it
  ends. A site the card did not name is refused, and so is every address
  inside the network the platform runs in.
- **Packages.** The platform installs the packages the card named; the
  program never reaches where packages come from. The same list is
  kept for the next run that names it.
- **Credentials.** The person is asked for each one on a card of its
  own, and it is saved with their secrets under that site. The program
  reads it from its environment. A credential is always for one of the
  sites the card named, and its value is taken out of whatever the
  program printed before that is given back.
- **Files.** The files named in the call are in the program's working
  folder under their own names. Every file the program makes there is
  handed back, up to ten.

The program runs as the agent does where the platform confines agents:
a user of its own, its files fenced, one way out. It has five minutes
(`DECENTAI_CODE_RUN_SECONDS` changes that for a deployment), no
terminal, and nobody to ask.

Where the platform does not confine agents, the sites are not held to
the card: the agent's page says when that is so.

## Function

| Function | Level | What it does |
|---|---|---|
| `program.run` | 2 | Writes a program for the goal, puts it before the person, runs it when they allow it, and returns what it printed and the files it made |

| Input | |
|---|---|
| `goal` | What the program should do, in full: it cannot ask anything |
| `files` | The files it reads, by ref |
| `code` | A program to start from, for changing one that ran before |

| Outcome | |
|---|---|
| `done` | It ran: `output` is what it printed, `files` what it made |
| `declined` | The person did not allow the program, or did not give a credential it needs |
| `unanswered` | Nobody answered the card |
| `failed` | No program could be written for the goal, or three in a row failed; `summary` says which |

## What it needs from the platform

A platform with the code card (`call.propose`), and a function allowed
to run code (`code: true`): DecentAI from 2026-09-30.

## Tests

`tests/test_code_runner.py` runs the agent in a real worker: programs
are really written to disk and really run. The chat's model is
scripted, and the person is a scripted answer to the code card and to
a credential.
