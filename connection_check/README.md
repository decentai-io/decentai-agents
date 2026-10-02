# Connection Check (`connection_check`)

Someone installs DecentAI and reads that an agent reaches the hosts it
declared and nothing else. They ask this agent to show it: it tries
the one host it declared, one it never declared, an address inside the
network and a way around the platform, and says what happened to each.

No credential, no records, no packages. An attempt is a connection
opened and closed: nothing is sent to a host it tries, and nothing is
read from one.

## Functions

| Function | Level | What it does |
|---|---|---|
| `check.report` | 0 | The whole check, five attempts. Each row says what was expected of an agent that is held and what happened; `held` says whether every row went as expected, and `verdict` says it in a sentence |
| `check.reach` | 0 | One host the person names: a name, a name and a port (`host:8443`), or a whole http or https address, of which the host and the port are used. Says whether this agent declared it, what happened, and the platform's reason when it was refused |

## What the check tries

| Attempt | Target | A held agent |
|---|---|---|
| The host this agent declared | `example.com` | reaches it |
| A host it did not declare | `www.wikipedia.org` | is refused |
| An address inside the network | `169.254.169.254` | is refused |
| Past the proxy: a connection made straight | `1.1.1.1:443` | is refused |
| Past the proxy: a name looked up | `example.com` | is refused |

## Four outcomes

| Outcome | Means |
|---|---|
| `reached` | the connection was opened |
| `refused` | the platform stopped it; `reason` is in the platform's own words |
| `unreachable` | it was allowed, and the host did not answer |
| `not_tried` | this agent would not try it |

## What it says where nothing holds an agent

On a developer's machine, and on any deployment that does not confine
agents, a worker is pointed at no proxy and connects as any program
does. The check says so: `through_the_proxy` is false and the verdict
is that nothing holds this agent to the host it declared. There it
does not try an address inside the network at all, since nothing would
have refused it.

Where the proxy is offered and nothing makes it the only way out, the
verdict is *partly held*: what was not declared is refused by the
proxy, and the connection made past it was not stopped.

## Where it connects

`example.com`, and the whole of it. Everything else it tries is meant
to be refused.
