# Control Centre Specification

The control centre is the local web interface for the whole assistant: live
state, memory, conversation, tools, security, LLM routes, technical readings,
and settings. The daemon serves it in-process.

## Design Principles

1. **One process.** The server runs on a thread inside the daemon, so every
   view reads the live objects the voice path is using. There is no IPC, no
   mirrored state, and nothing to keep in step.
2. **Offline.** No CDN, no web fonts, no analytics, no outbound request of
   any kind. The interface is plain HTML, CSS, and JavaScript served from
   `static/`, with a system font stack.
3. **No build step.** Editing a file and reloading the page is the whole
   development loop.
4. **Failure is local.** A control centre that cannot start prints why and
   the daemon carries on. Serving dashboards is never allowed to stop the
   assistant answering.
5. **Read live, write through the same doors.** Mutations go through the
   same functions the voice path calls, including the security gate.

## Visual language

Every colour, type size, spacing step, and duration the interface uses is
named in `static/css/tokens.css`. A view that needs a shade it cannot find
gains a token rather than a literal, so the whole instrument is retuned from
one file and no view can drift from the rest.

| Group | Rule |
|---|---|
| Surface | Four depths: the page, a card above it, a well recessed inside a card, and the raised controls within either |
| Colour | One accent, for what is active, focused, selected, or newly arrived. Three status tones, each with a text, fill, and border value so a chip, a rail, and a meter read the same |
| Type | An eight-step scale. Headings, labels, and readings are chosen from the ladder rather than per view |
| Motion | Transitions mark a change of state, never decorate one. `prefers-reduced-motion` disables every animation and transition outright, and anything painted from JavaScript, which that rule cannot reach, asks `motionAllowed()` for itself |
| Overflow | A region that scrolls is sized against the window rather than a fixed count of pixels, and pins its heading above it, so a partly visible row reads as more below rather than as a rendering fault |
| Shared parts | A component two views use is named for what it is rather than for whichever view needed it first, and lives in `app.css` rather than beside one of them |

The sidebar groups its eleven destinations under three names: what is
happening now, what the assistant knows, and how the machine is set up. Each
group is an ARIA group carrying that name, so the structure is available to a
screen reader and not only to the eye.

## Runtime

| Aspect | Behaviour |
|---|---|
| Entry point | `jarvis.webui.start_from_settings(cfg)`, called by `daemon.main()` right after the models are named and before Whisper loads, so the interface is reachable while startup is still running |
| Standalone | `python -m jarvis.webui` serves memory, settings, and stored telemetry with no daemon. Live state is empty because nothing is listening |
| Server | `werkzeug.serving.make_server(..., threaded=True)` on a daemon thread. Threaded because a server-sent event stream holds its connection for as long as the page is open |
| Shutdown | `WebUIServer.stop()` from the daemon's cleanup path, both on the normal exit and after a smoke test |

## Request guards

The interface can approve tool execution and rewrite `config.json`. A
loopback port is therefore not automatically safe: any page open in the same
browser can post to it.

| Guard | Rule | Stops |
|---|---|---|
| Host allowlist | While bound to loopback, the `Host` header must name a loopback address | DNS rebinding: a hostile domain resolving to 127.0.0.1 |
| Write header | Every method outside GET/HEAD/OPTIONS must carry `X-Jarvis-UI: 1` | Cross-site form posts, which cannot set a custom header |
| Token | Required for every request once the bind address leaves loopback. Supplied as `X-Jarvis-Token` or `?t=` | Anyone else on the network |

No response carries `Access-Control-Allow-Origin`. Every response carries
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

### The microphone socket

`GET /api/voice/stream` upgrades to a WebSocket carrying 16-bit mono PCM
from the browser's microphone into the listener's audio queue. It cannot
rely on the guards above: an upgrade is a GET, so the write header does not
apply to it, and WebSockets are exempt from the same-origin policy, so any
page in any tab can open one against a loopback port. A socket that speaks
into a tool-running assistant needs more than "you reached the port".

It therefore checks `Origin` itself. A page must name this control centre,
on this host, on this port. An absent header is allowed, because only a
browser sends one and nothing can trick a script into carrying someone
else's. `null`, the sandboxed-frame origin, names no site that could be
checked and is refused.

Frames larger than a capture chunk are dropped rather than forwarded, and a
frame the listener refuses (Jarvis is speaking, the queue is full, nothing
listening) closes nothing: the socket stays open for the next one.

The allowlist and the token divide the work between them. On loopback the
set of valid host names is known, so the allowlist is exact and no token is
needed: reaching the port already means reaching the machine. Off loopback
the valid names are whatever the LAN calls this machine, which cannot be
enumerated, so the token becomes the guard and the allowlist stands down.

### Token lifetime

`resolve_token(bind_host, configured)`:

- a configured token is used as written
- an empty token on loopback stays empty
- an empty token off loopback mints a fresh one per start and prints it

A minted token is deliberately not written back to `config.json`: a token
that lives only for one run cannot leak from a file that is edited, synced,
or shared.

### Secrets

`/api/settings` and `/api/system` never return a credential in clear text.
Values are masked to their last four characters. Writing a credential is
allowed; reading one back is not.

## API

| Route | Serves |
|---|---|
| `GET /api/health` | Liveness, port, bind address |
| `GET /api/status` | Phase, uptime, tallies, last turn, models, audio |
| `GET /api/logs` | Recent local diagnostic entries, with credentials redacted |
| `GET /api/events` | Server-sent events. Opens with the current state so a page that connects mid session is correct at once |
| `GET /api/turns` | Recent turns with their stages and tool calls |
| `GET /api/turns/export.csv` | The same history flattened, one column per stage |
| `GET /api/conversation` | Recent turns, the discarded-utterance counts, and whether conversation mode is on |
| `POST /api/conversation/mode` | Hold the follow-up window open, or let it close. 409 when nothing is listening |
| `GET /api/passive` | The passive record, filtered by day, plus the switch state |
| `POST /api/passive/enabled` | Flip the running recorder and persist the choice |
| `DELETE /api/passive/<id>`, `?date=`, `?all=1` | Delete one line, one day, or the whole record |
| `POST /api/chat` | Put text through the reply engine, optionally spoken aloud |
| `GET /api/memories`, `/api/topics`, `/api/meals`, `/api/stats` | The diary, topic tally, meal log, and memory statistics |
| `GET/POST/PUT/DELETE /api/graph/*` | The memory graph and its presets |
| `POST /api/graph/import-diary` | Feed every diary summary through graph extraction and placement, streaming NDJSON progress |
| `POST /api/graph/consolidate-all` | Rewrite every populated graph node with the current merge rules, streaming NDJSON progress |
| `POST /api/diary/scrub-deflections` | Rewrite diary summaries without deflection narration, streaming NDJSON progress |
| `POST /api/diary/optimise-topics` | Normalise topic tags across diary rows, streaming NDJSON progress |
| `GET /api/tools`, `POST /api/tools/refresh` | The tool catalogue, MCP server state, rediscovery |
| `GET /api/security`, `/api/security/pending`, `POST /api/security/decide` | The confirmation policy, what is waiting, and the answer |
| `GET /api/system` | GPU, resident models, speech configuration, paths, process |
| `POST /api/system/restart` | Ask the daemon to tear down and start a fresh generation in place |
| `GET/PUT /api/settings` | Every editable config field, and writes to it |
| `GET /api/llm/routes` | FAST, CHAT, and PRIVATE chains with masked credentials and persisted health state; performs no outbound request |
| `POST /api/llm/routes/probe` | User-triggered model catalogue and credential probe |
| `POST /api/llm/routes/reset` | Clear persisted cooldowns and process-local invalid-key marks |
| `PUT /api/llm/routes` | Validate and replace generic route configuration while preserving unchanged masked credentials |
| `GET /api/crew` | One reading of the NAS-hosted agent crew: recent activity, the agent roster with its tallies, a 14-day daily activity count, and when the reading was taken |
| `POST /api/crew/chat` | Relay one message to one crew agent and return its reply |

`POST /api/chat` runs one turn at a time, and the turn it waits for may not
be its own. Voice, the desktop chat window and this endpoint all reach the
same reply engine against the same dialogue memory, so all three take the
daemon's shared query lock (`daemon.chat_query_lock`). A request that finds
it held, by a spoken turn or a typed one, is refused with 409 rather than
queued: a person typing cannot outrun a turn. With the daemon running, a
typed turn joins the spoken conversation; standalone, the control centre
keeps one of its own.

`PUT /api/settings` follows the same two rules as the Qt settings window,
because both write the same file: only non-default values are stored, and
keys the registry does not describe survive untouched. A credential is sent
back masked, and a masked value returned unchanged leaves the stored one
alone, so saving a form never overwrites a secret with its own mask.

The Settings view carries a restart control alongside Save, always available
rather than conditional on a changed field, because config the daemon read
at start-up (models named, the webui's own bind settings, anything else the
running objects captured once) only takes effect on a fresh generation.
`POST /api/system/restart` calls `jarvis.daemon.request_restart()`, which
shares `request_stop()`'s exact shutdown path and then starts another
generation in the same process rather than letting it end — see "Restarting
in place" below. The endpoint returns before that happens; the page polls
`/api/health` until it answers again, then reloads.

### Restarting in place

The daemon's `main()` runs generations in a loop rather than exiting after
one. A generation that stopped because of `request_restart()` starts
another one in the same process and thread (or subprocess, however this
run was launched); every other stop returns from `main()` as before. No
process is replaced and no new one is spawned, so a supervisor watching
this process (the desktop tray's `daemon_process`/`daemon_thread`, or
nothing at all for a bare terminal launch) sees one long call rather than
an exit — the tray's crash/stopped-unexpectedly handling never fires for a
requested restart.

## Overview

The only page that reads across the others, so what it holds is a reading
per destination and a way into each. It keeps no turn history of its own:
the Conversation view holds the turns and holds them as a conversation, and
a second, worse copy here would only teach a reader that the two disagree.

| Band | Holds |
|---|---|
| Where the time went | The last turn's total, the median of the recent ones beside it, the recent totals as a sparkline, and the stage breakdown with its legend |
| Readings | Memory, tools, security, and discarded utterances. Each is a card that links to the view holding the detail |
| Last exchange | What was just said and what came back, and a way into the conversation |

The median rather than the mean: one turn that waited on a cold model would
drag an average somewhere no turn has ever actually been, and a single last
reading cannot say whether it was normal.

## LLM routes view

The LLM routes view displays the ordered FAST, CHAT, and PRIVATE chains. Each
entry keeps active state, protocol, model, masked credential, hit and failure
counts, block time, and the last safe error label within its chain card. The
entry layout wraps long model names and error labels instead of overflowing
into neighbouring chains. The PRIVATE chain is read-only and contains one
loopback Ollama route. Configured FAST and CHAT entries are editable using
only the route schema described by the LLM spec.

Loading and refreshing the view reads local config and cooldown state only.
The only control that contacts a configured endpoint is **Probe models**.
Resetting cooldowns and saving routes are local file writes.

## Logs view

The Logs view is the browser-based diagnostic surface. It polls the recent
in-process diagnostic ring every two seconds and renders every entry as text,
never HTML. The ring holds at most 500 entries and returns at most 500 entries
per request. Credential-like values are redacted before they enter the ring
and are redacted again by the normal response safety layer.

An entry carries a time, a category, and a message, and nothing else. It has
no severity, so the view invents none: no line is coloured as though it were
a failure, because the ring has no way of knowing that it is. The two
dimensions an entry actually has are what the view offers.

| Control | Behaviour |
|---|---|
| Category | The categories present in the log, and only those, so the row never offers a filter that would empty the view |
| Search | Narrows on the message text as it is typed |
| Follow | Keeps the newest entry in view. It follows the reader as well as the button: scrolling back turns it off, and scrolling to the end turns it on again, because scrolling back is how someone says they are reading rather than watching |

Each line puts its time and category in fixed columns so both can be skipped
over, which is how a log is read. Filtering never asks for anything: it
selects from the entries already on the page and says how much of the log is
showing.

The view is intentionally diagnostic rather than a full terminal mirror. It
contains events emitted through `jarvis.debug.debug_log`, including listening,
model, and route diagnostics, while the desktop face remains the everyday
voice interaction surface.

## Conversation view

Three bands, and the exchange is the one the view is for.

| Band | Holds |
|---|---|
| Live | This browser's microphone, what Jarvis is doing, and the conversation-mode switch. Under them, the utterances thrown away, and only when there are any |
| Exchange | Every turn as a dialogue on one speaker column, grouped by day, oldest first, with what each turn cost folded away behind a disclosure |
| Composer | Typing a turn, and whether to say the answer aloud |

The view fills the window rather than growing past it, so the exchange is
the only thing on the page that scrolls and the composer stays where it is.

### Two live facts, never merged

The band reports the microphone and the phase side by side because they are
true at different scopes. The phase is the daemon's own and would be true
with this page closed. The microphone is this browser's, and is true only
here. A view that ran them together would report that the assistant is
listening when nothing had opened a microphone at all.

### What "live" means here

- **Phase.** The `phase` event, with the same dot the header uses. The
  view reads `/api/status` on mount as well, so it is correct from the
  moment it appears rather than from the next change, which on an idle
  assistant may never come.
- **Stage.** The `stage` event names which part of the turn the wait is
  in. The phase cannot: "running a tool" is true of every tool there is.
- **The wait.** How long the current phase has been running, counted while
  a turn is in flight and held still at idle, where it shows the last wait
  that finished instead.
- **Arrival.** A turn whose id was not in the previous reading is marked
  once, for as long as the glow lasts, and never again. A first load marks
  nothing.
- **Level.** While the microphone is open, the loudest sample in the frames
  already going to the daemon. It opens no second capture, keeps no audio,
  and changes neither what is sent nor when.

The exchange scrolls to the newest turn on arrival only if the reader was
already there. Someone reading back through the history is not dragged to
the bottom because a turn finished elsewhere.

### Motion that the stylesheet cannot reach

`tokens.css` switches off every CSS animation and transition for a reader
who asked for less motion, but a graphic painted from JavaScript is
neither: a bar whose height is assigned on a timer keeps moving through
that rule. `motionAllowed()` in `ui.js` is what anything painted that way
asks, and the level meter is not built at all when the answer is no. What
it shows is written beside it in words either way, so nothing is carried by
motion alone.

### Conversation mode

The band carries the switch that holds the follow-up window open, so no
question needs the wake word, and the header carries an indicator beside
the phase on every view: an open microphone is a state worth seeing from
wherever the page happens to be. Both follow the `conversation` event
rather than the last thing the page clicked, because the mode also ends on
its own when the user asks Jarvis to stop.

Standalone there is no voice loop, so the switch reaches nothing and says
so instead of showing a mode that is not running anywhere.

## Passive record

While passive capture is on, the header carries a recording indicator beside
the phase on every view, so a page open at any depth still says whether the
room is being written down. It is driven by the `passive` event rather than
polling, and shows nothing at all while the switch is off.

The record has a view of its own rather than a section inside another one.
It is a privacy surface before it is a reading, and it grows without limit:
an account of every word spoken in the room cannot sit on a page that is
also meant to show a conversation.

The view carries the switch, the state, and the count still waiting to be
digested in its frame, then the record itself: lines grouped by day with
their time and text, a mark on the ones that were addressed to the
assistant, and delete controls for a line, a day, and the whole record. The
mark is a rule in the gutter carrying its own accessible name rather than a
label on every line, because on a busy day most of the record is addressed
to the assistant and a label repeated that often stops being read.

Turning the switch on asks first and names the model backend that will see
the ambient text, because whether that is local depends on what
`llm_provider` points at. Turning it off asks nothing: permission is owed
for starting to write the room down, not for stopping. Deleting states
plainly that it removes the transcript and not what has already been folded
into the diary or the graph, each of which has its own delete path in the
Memory view.

See `../listening/passive_capture.spec.md`.

## Memory view

The Memory view places the graph tree and selected-node editor side by side,
followed by a Maintenance section, the diary, and a responsive two-column row
for the meal log and topic tally. The meal log shows each meal's date,
description, energy, protein, carbohydrate, and fat. The topic tally shows
each normalised topic with the number of diary entries that carry it. All
stored content is rendered as text nodes.

The Maintenance section exposes four long-running local-model actions:

| Action | Route | Confirmation |
|---|---|---|
| Import diary | `POST /api/graph/import-diary` | Not required; graph facts are added through the normal learning pipeline |
| Consolidate graph | `POST /api/graph/consolidate-all` | Explains that every populated graph node is rewritten |
| Clean deflection narration | `POST /api/diary/scrub-deflections` | Explains that stored diary summaries are rewritten and other text is preserved |
| Optimise topics | `POST /api/diary/optimise-topics` | Explains that stored topic tags are rewritten while diary text stays unchanged |

Each action disables the maintenance controls while it runs and consumes its
NDJSON stream incrementally. Its card shows the processed and total counts, a
live progress bar, and a result summary using the endpoint's action-specific
completion fields. Network failures and streamed `error` events remain visible
in the same status area. The memory data is re-read after a completed action.

## Mission Control view

Mission Control reads the activity log of, and can relay a chat message to,
an agent crew that runs outside this daemon entirely, on a separate machine
(a NAS), unrelated to the security gate and to every other view here. The
daemon holds no direct route to that machine's database or its chat engine;
it calls a small NAS-side HTTP endpoint for both, guarded by a shared key
sent as `X-Crew-Key`.

The NAS is not always on. A reading distinguishes three states rather than
collapsing them into one empty view:

| State | Reported as | Shown as |
|---|---|---|
| No endpoint configured | `configured: false` | A message pointing at Settings |
| Endpoint configured, no answer within the request timeout | `configured: true, reachable: false` | A message saying the NAS is not answering |
| Endpoint answered | `configured: true, reachable: true`, plus `entries`, `agents` and `daily` | The activity ribbon, the agent roster, and the activity feed |

A connection failure, a timeout, and a reply that fails to parse as JSON are
all treated the same: `reachable: false`. The view never fabricates a
reading it does not have.

### Taking the reading

The daemon takes one reading for everyone watching and publishes it as a
`crew` event, rather than each open page asking the NAS on its own timer.
Two tabs would otherwise mean twice the traffic to a device that is often
asleep, and no page could tell how old the answer in front of it had become.

The poller contacts nothing unless a crew endpoint is configured **and** the
event bus has at least one subscriber. With the control centre closed there
is no outbound request at all. A reading that fails is logged and dropped;
the poller is never allowed to die, because Mission Control already has an
honest way to say the NAS is not answering.

`GET /api/crew` takes the same reading on demand, so a page that has just
opened is correct before the next tick rather than blank until it.

### What a reading carries

| Field | Meaning |
|---|---|
| `checked_at` | When this reading was taken, as epoch seconds. Present in every state, including the two that reach nothing |
| `entries` | The activity log as the NAS returns it, newest first |
| `daily` | `entries` bucketed by calendar day in UTC over a fixed trailing 14-day window, zero-filled, oldest first, each day split by outcome as well as totalled. A fixed width regardless of how busy the crew has been, so the ribbon never resizes on its own |
| `agents` | One entry per agent: per-status tallies, `total`, `last_at`, `last_status`, and `daily` as bare counts positioned against the same days as the reply's own `daily` |

Status and freshness are separate facts. A tally is only as true as the
moment it was read, so `checked_at` is reported alongside it rather than
folded into it, and the view states the age of what it is showing.

The `agents` list is the configured roster (`crew_agents`), in that order,
followed by any agent that logged work without being listed. The activity
log only names agents that have done something, so without a roster an idle
agent would vanish and read as though it never existed; an agent with no
entries is reported with zero counts and a null `last_at`, which the view
shows as quiet. An unlisted agent is appended rather than dropped, because
hiding real activity is the worse failure.

### What the view shows

Three bands, over one reading:

| Band | Holds |
|---|---|
| Summary | How much of the roster is working, the fortnight's total, the success rate, and how long ago the last thing happened. Under them, fourteen days as bars stacked by outcome |
| Roster | One card per agent: last outcome, how long ago, its own fourteen days, and the share that succeeded. A card selects that agent |
| Activity | The log as a feed on a single rail, grouped by day, each entry carrying its full text rather than a truncated column |

The feed can be narrowed to one agent, to failures, or to both. Filtering
never asks for anything: it selects from the reading already on the page,
and says how much of it is showing.

### What "live" means here

An agent logs a line when it *finishes* something. Nothing in the log says
what an agent is doing right now, so the view does not imply that it knows.
What is genuinely live is shown and nothing else is:

- **Freshness.** How long ago the reading was taken, counted up every
  second, beside a state that never stands in for it.
- **Recency.** How long ago each agent last worked, and each entry, on the
  same second-by-second count.
- **Arrival.** An entry whose id was not in the previous reading is marked
  once, for as long as the glow lasts, and never again. A first load marks
  nothing, because everything would be marked and the signal would mean
  nothing.
- **Silence.** An agent with nothing in the window is dimmed and says so.

Nothing else on this view moves. A marker that pulses without a change of
state behind it is decoration, and decoration on an operations view reads
as information that is not there.

### Chat relay

`POST /api/crew/chat` takes `{"agent": ..., "message": ...}` — `agent` is one
of the roster `askCrew`/`checkCrewReplies` already use
(`AGENT_THREADS` in `../tools/builtin/ask_crew.py`), reused here rather than
duplicated. The daemon validates the agent name and that a message was
given, then forwards both to the NAS's own `/chat` endpoint with the same
`X-Crew-Key` header the read path already sends, and relays back whatever
that endpoint returns.

This module never talks to the crew's chat engine directly and never
persists anything about the exchange — the NAS-side endpoint is the one
thing that proxies on to it. A connection failure, a timeout, or the NAS
reporting its own upstream error all collapse to the same shape the read
path already uses (`reachable: false` plus a plain-language `error`),
never a 500 to the browser. The chat panel keeps its own client-side log of
what was sent and received for the session; nothing about a conversation is
written to this daemon's own storage.

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `webui_enabled` | bool | `true` | Serve the control centre with the daemon |
| `webui_port` | int | `5055` | Listening port. Anything outside 1024-65535 falls back to the default rather than failing the start |
| `webui_bind_host` | str | `"127.0.0.1"` | `0.0.0.0` reaches it from the same network and turns the token on |
| `webui_token` | str | `""` | Empty mints one per start when off loopback |
| `webui_open_browser` | bool | `false` | Open the interface at daemon start |
| `crew_api_url` | str | `""` | Base URL of the NAS crew endpoint. Empty hides the Mission Control view |
| `crew_api_key` | str | `""` | Shared key sent as `X-Crew-Key` |
| `crew_agents` | list | The seven crew roles | Who Mission Control shows, in display order. Emptying it restores the default rather than hiding the crew |
