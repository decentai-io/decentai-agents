# Places and Distances (`places`)

You are meeting a client near Dubai Marina tomorrow morning. Ask for a
quiet coffee place that is open, its hours and phone number, and how
long the drive is from the office at 8am. The assistant answers from
Google Maps — every name, address, rating and time is Google's — and
says the travel time is Google's estimate at the moment it asked.

Finds places by text or near a point, reads one place's address,
phone, website and opening hours, and estimates distance and travel
time from one origin to many destinations. Google Maps Platform over
the organization's API key: Places API (New) and Routes API. Every
function is a read; nothing is kept.

## Functions

| Function | Level | What it does |
|---|---|---|
| `account.status` | 0 | Confirms the key works for each API; names the API to enable, or the problem, when it does not |
| `places.search` | 0 | Free-text search ("coffee near Dubai Marina"), optional location bias and `open_now`; up to 20 rows |
| `places.nearby` | 0 | Places of given types within a radius of a point or a place, nearest first; up to 20 rows |
| `places.details` | 0 | One place: address, phone, website, opening hours per weekday, open now, Maps link |
| `routes.distance` | 0 | One origin to up to 20 destinations: km, minutes, minutes in traffic when driving at a stated time, directions link |
| `routes.between` | 0 | One trip with its summary (main road), Google's warnings and the first 20 steps |

A row carries only what Google returned: a place with no rating has no
`rating`, never a zero. `straight_line_km` in `places.nearby` is the
straight line between Google's coordinates, not a travel distance.

A location in `routes.*` is exactly one of `address`, `place_id` (from a
search) or `lat` and `lng`. `departure_time` is ISO 8601 with an offset;
the agent does not guess a time zone. Driving with a departure time
asks Google for traffic: `duration_in_traffic_minutes` is the estimate
with traffic, `duration_minutes` the same trip without it. Every routes
answer carries `estimated_at`, the moment it was asked.

No records: saved places ("the office") are left to the conversation
for now; a small record type would earn its place once people ask for
the same origin every day.

## Setup

1. In a Google Cloud project of the organization's, with billing
   enabled, enable **Places API (New)** and **Routes API** (APIs &
   Services → Library). The older "Places API" and "Distance Matrix
   API" are different products; this agent uses neither.
2. Create an API key (APIs & Services → Credentials → Create
   credentials → API key).
3. Restrict it: under **API restrictions** choose "Restrict key" and
   select only Places API (New) and Routes API. The key is called from
   the platform's servers, so an application restriction by IP address
   is the right one if the deployment's egress address is fixed.
4. Paste the key into the agent's Credentials tab as **Google Maps
   Platform key**, grant it to this agent, and ask the assistant to
   check the key — `account.status` names any API still switched off.

`api_base_url` is for tests only; leave it empty.

## Billing, honestly

**These APIs are billed per request, to the Google Cloud project the
key belongs to — the organization's, not the person asking.** Google
bills each call by the most expensive field its field mask names, so
every call here names only the fields it reads (never `*`). As Google
documents its SKUs at the time of writing — check Google's pricing
page, which changes:

| Call | Fields asked | SKU tier it falls in |
|---|---|---|
| `account.status` | Text Search `places.id`; one route matrix element | Text Search Essentials (IDs only); Route Matrix Essentials, one element |
| `places.search` | id, name, address, location, Maps link, **rating, rating count, price level, open now** | Text Search Enterprise — the rating, price and open-now fields are what lift it there |
| `places.nearby` | the same row fields | Nearby Search Enterprise; a `place_id` centre adds one Place Details Essentials call (`location` only) |
| `places.details` | the row fields plus phones, website, weekday hours, business status | Place Details Enterprise |
| `routes.distance` | distance, duration, static duration, condition | Route Matrix Essentials per destination; **Pro** when driving with a departure time (traffic) |
| `routes.between` | distance, durations, description, warnings, step distances and instructions | Compute Routes Essentials; **Pro** with traffic |

Google's monthly free usage per SKU applies first. The agent's
instructions tell the assistant not to repeat a search whose answer it
already has, but nothing here caps spend: set quotas on the APIs in the
Cloud console if the organization wants a ceiling.

## Errors

| `kind` | Meaning |
|---|---|
| `auth` | The key is invalid, an API is not enabled (named), the key's restrictions block an API (named), or billing is off — a person fixes the setup |
| `not_found` | Google has no place with that id |
| `http` | Google refused the request (its message is passed on), the quota is used up, or Google could not be reached after one retry |

A missing credential is a plain answer from `account.status`
(`connected: false`) and `kind: auth` from everything else.

## Limits, and what is not verified

- Live Google Maps Platform behaviour is **not yet verified**: the tests
  run the agent in a real worker against a loopback stub
  (`tests/maps_stub.py`) that refuses a request without a key or a field
  mask and returns only the fields a mask names. The request and
  response field names follow Google's REST reference as written; they
  have not been exercised against the real APIs.
- Results are capped at 20 places and 20 destinations per call, text
  fields clipped.
- One origin per `routes.distance` call; no waypoints, no alternative
  routes, no tolls or fuel options.
- A past `departure_time` is refused for everything but transit.
- Directions links are built from the inputs in Google Maps URL form;
  the link opens Google Maps, which plans the route afresh.
