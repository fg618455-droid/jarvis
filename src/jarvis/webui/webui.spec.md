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
| Selected against status | What is selected or active is the accent as a rule: a border on one edge, accent text, at most `--accent-soft` behind it, never a filled chip. What carries a status is always a filled chip with its own fill, border, and glyph. The two are therefore different shapes before they are different colours, which is what makes them separable in a warm palette where accent and warning are neighbours, and for a reader who cannot tell those two hues apart at all |
| Type | A seven-step scale. Headings, labels, and readings are chosen from the ladder rather than per view |
| Motion | Transitions mark a change of state, never decorate one. `prefers-reduced-motion` disables every animation and transition outright, and anything painted from JavaScript, which that rule cannot reach, asks `motionAllowed()` for itself |
| Focus | Anything that can hold focus shows a ring when a keyboard put it there, and the ring clears 3:1 against both the control and what the control sits on. A field may also warm its border on focus, but a border is an addition to the ring and never a replacement: a rule suppressing the outline on a field is more specific than the shared `:focus-visible` one and silently disarms the keyboard everywhere at once |
| Overflow | A region that scrolls is sized against the window rather than a fixed count of pixels, and pins its heading above it, so a partly visible row reads as more below rather than as a rendering fault. Where the container is too narrow to hold the columns, the rows stack and each value carries its own column name instead, so a sliced record is still labelled |
| Width | A view lays itself out against the box it is in, not against the window. The same module is a full-width page at one address and a column inside a panel at another, so its layout rules are container queries on `view`; only the shell around it, the header, the deck, and the page gutter, is sized against the window |
| Shared parts | A component two views use is named for what it is rather than for whichever view needed it first, and lives in `app.css` rather than beside one of them |

### Themes

`tokens.css` has two halves and the split is the point. `:root` holds the
instrument: the type ladder, the spacing steps, the radii, the motion
durations, and the sizes the layout is built from. A `[data-theme]` block
holds only paint. A theme therefore changes what the interface looks like and
never where anything is or how large it reads, so no heading changes size
because someone preferred a different palette.

| Theme | Is |
|---|---|
| `graphite` | Near-black and one cool accent. The default, and what the interface has always looked like |
| `arc` | The same instrument under a colder light: a blue-white filament on deep slate, with the circular motif carrying more of the accent |
| `ember` | The same instrument in a warm light: a brown-black rather than a blue one, stepped surfaces, and one orange |

`ember` is written in OKLCH, because its surfaces are a ramp rather than
seven separate colours: stepped by lightness in OKLCH a surface stays the
same colour getting lighter, where the same step in HSL turns muddy through
the middle and has to be corrected by hand at every stop. A theme is free to
be written in whatever notation suits it; nothing reads these values except
the browser.

Three places name the themes and all three have to agree: `tokens.css` paints
them, `theme.js` offers them, and a small inline script in `index.html`
applies the remembered one before the first paint. That script cannot import
the module it is guarding against, so it carries its own copy of the list,
and `tests/webui/test_theme_tokens.py` holds the two in step. Forgotten
there, a theme is offered in the picker and refused on reload.

Adding a theme is adding one block and one row in `theme.js`. No view knows a
theme exists; every view reads `var(--accent)` and gets whatever the active
block says it is.

Every theme names the same tokens as every other one, because a token a theme
forgets does not fall back to a sensible default: it keeps whatever the last
theme painted, and one card quietly reads in the wrong palette.
`tests/webui/test_theme_tokens.py` holds that rule as a mechanism rather than
as a list of values, so it also holds for themes that do not exist yet.

The choice is this browser's, in `localStorage`, and never reaches
`config.json`. It is a preference about looking at a screen rather than a
fact about the assistant, so two people on two machines reading the same
daemon can disagree about the palette without either writing to the other's
configuration. A small inline script in `index.html` applies it before the
first paint, because the module that owns it is deferred and the page would
otherwise flash the default on every load for anyone who changed it.

The System model card makes three different facts explicit. **Effective
routes** names the first currently available FAST, CHAT, and PRIVATE candidate
and labels each one local or remote. **Configured local models** names the
Ollama FAST fallback, CHAT fallback, PRIVATE, and embedding roles. **Actually
resident in Ollama** is populated only from `ollama ps` and carries the local
GPU reading beside it. A remote route model is therefore never presented as
if it consumed local VRAM. The LLM Routes view applies the same local/remote
label to every effective-chain entry.

## Runtime

| Aspect | Behaviour |
|---|---|
| Entry point | `jarvis.webui.start_from_settings(cfg)`, called by `daemon.main()` right after the models are named and before Whisper loads, creates `WebUIMode.DAEMON_ATTACHED`, so the interface is reachable while startup is still running |
| Standalone | `python -m jarvis.webui` and the desktop window's fallback server explicitly create `WebUIMode.STANDALONE`. They serve memory, settings, and stored telemetry with no daemon. Status carries `daemon_running: false`; phase, uptime, models, audio, and last turn are empty, so the shell says Jarvis is not running and hides recording and conversation indicators. `/api/turns` and CSV export read the persisted `turns.jsonl` (rotated generation first) without promoting those old turns into live status |
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
| `GET /api/health` | Control-centre liveness, port, bind address, explicit `daemon-attached`/`standalone` mode, and whether a daemon is attached |
| `GET /api/status` | Daemon presence, phase, uptime, tallies, last turn, models, audio |
| `GET /api/logs` | Recent local diagnostic entries, with credentials redacted |
| `GET /api/events` | Server-sent events. Opens with the current state so a page that connects mid session is correct at once |
| `GET /api/turns` | Recent live turns when attached; persisted journal plus this process's new turns in standalone |
| `GET /api/turns/export.csv` | The same mode-aware history flattened, one column per stage |
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
| `GET /api/tools`, `POST /api/tools/refresh` | The tool catalogue, MCP server state, rediscovery, and each server's latest retained discovery error |
| `GET/PUT /api/mcp/servers` | The configured MCP servers, how each launches, whether it connected, and the editor's schema. Writes replace the set, preserving unchanged masked credentials |
| `GET /api/briefing` | Today's School items, the cached prose if it exists, and the spoken briefing's own state. Reads no model |
| `POST /api/briefing/refresh` | Generate today's prose through the spoken briefing's own generator and cache it for the local day |
| `GET /api/security`, `/api/security/pending`, `POST /api/security/decide` | The confirmation policy, what is waiting, and the answer |
| `GET /api/system` | GPU, effective routes with locality, configured local model roles, actual Ollama residency, speech configuration, paths, process |
| `POST /api/system/restart` | Ask the daemon to tear down and start a fresh generation in place; 409 in standalone mode |
| `GET/PUT /api/settings` | Every editable config field, and writes to it |
| `GET /api/llm/routes` | Configured routes, their editor schema, and separate effective FAST, CHAT, and PRIVATE chains with masked credentials and persisted health state; performs no outbound request |
| `POST /api/llm/routes/probe` | User-triggered model catalogue and credential probe |
| `POST /api/llm/routes/reset` | Clear persisted cooldowns and process-local invalid-key marks |
| `PUT /api/llm/routes` | Validate and replace generic route configuration while preserving unchanged masked credentials |
| `GET /api/crew` | One reading of the NAS-hosted agent crew: recent activity, the agent roster with its tallies, a 14-day daily activity count, and when the reading was taken |
| `POST /api/crew/chat` | Relay one message to one crew agent and return its reply |
| `GET /api/visualizer/state` | The face's `idle\|listening\|thinking\|speaking` reading, a waveform, and the two signals Jarvis never sets (`alert`, `loading`) |

There is deliberately no `/api/telemetry` alias: `/api/status` is live
session state and `/api/turns` is turn history, and conflating them would make
standalone persisted records look like daemon activity.

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

The shared field registry also describes structured object lists. The cloud
TTS chain uses that shape, so each ordered provider is editable as named
controls for provider, credential environment variable, voice, model, enabled
state, and timeout. The API accepts only the nested keys in that schema.
The Local AI & Behaviour category contains labelled local-model, timeout, and
thinking sections and links to the authoritative LLM Routes editor. Provider
connections, route models, backend override, and crew route selection are not
duplicated in general Settings. Speech Input owns microphone, wake-word, and
VAD/endpointing sections; Speech Recognition owns Whisper; Speech Output owns
common controls, the cloud chain, Piper, Chatterbox, and Kokoro as labelled
sections. The API returns each field's section label so the web and Qt forms
render the same structure.

The Settings view carries a restart control alongside Save whenever a daemon
is attached, rather than making it conditional on a changed field, because config the daemon read
at start-up (models named, the webui's own bind settings, anything else the
running objects captured once) only takes effect on a fresh generation.
`POST /api/system/restart` calls `jarvis.daemon.request_restart()`, which
shares `request_stop()`'s exact shutdown path and then starts another
generation in the same process rather than letting it end — see "Restarting
in place" below. The endpoint returns before that happens; the page polls
`/api/health` until it answers again, then reloads. Standalone mode disables
the control and the restart endpoint refuses the request instead of reporting
a restart that cannot occur.

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

## The deck

The interface is one place. The face is the page rather than a destination
inside it, every reading is a widget in a rail either side of it, and a
detail that needs room opens over the right rail instead of replacing the
page. Settings is the only thing that takes the whole window, because
editing the configuration is the only task that is not about watching the
assistant work.

| Region | Holds |
|---|---|
| Left rail | Today, System, Memory, Security, and the passive record. What the assistant knows and what state it is in |
| Centre | The face, the assistant's name, what it is doing in words, and the dock that speaks or types to it |
| Right rail | The last exchange, then Tools, MCP servers, LLM routes, Mission Control, and Logs as tiles under it, one to a row. How the machine is wired |
| Panel | Whichever detail is open, over the right rail |

The deck is sized against the window rather than flowed down it. Only a rail
and an open panel's body scroll: a deck that grew past the bottom of the
screen would put the face somewhere you have to scroll back to, which is the
one thing this layout exists to prevent.

Being sized against the window, a rail has to fill it. Every card in a rail
takes an equal share of its height, so the scale of a card is the rail
divided by what is in it rather than a gap someone left underneath. Two
things are exempt.

The last exchange takes its own height rather than a share. It is three
lines that do not wrap, so it is the same three lines tall whether it is
showing a turn or saying there has not been one; given a share of the rail
it would be a tall box holding one line of text, which is the hole the rail
was packed to close with a border drawn round it. What it does not take goes
to the readings under it rather than back to the page, and because its
height never changes the rail never rearranges itself the first time anyone
speaks.

And a tile is never given more room than a card beside it: it carries one
number where a card carries a number and a line about it, so it comes out
shorter at every window the deck is used at.

Below 1240px the right rail stops being a rail and folds into a row under
the deck. There is no height there to share, so the tiles lay out across the
width and take their own.

### Widgets

A widget is a reading and a way into the detail behind it, not a small
version of its panel. It answers one question at a glance and the panel
answers the rest.

Every widget paints from one shared snapshot the deck fetches for all of
them, so eleven readings on screen are not eleven timers against the daemon
and every widget is looking at the same moment rather than at eleven
slightly different ones. Mission Control is the exception in the other
direction: it is read once and then follows the `crew` event, because the
daemon already takes one reading for everyone watching and the machine it
reaches is often asleep.

A widget never invents a reading. A source that failed or has not answered
yet shows an em dash, because a zero meaning "no answer" and a zero meaning
"none" are very different facts on the security widget.

A status chip is toned by what it says rather than by something beside it,
and two facts that are true at different times get a chip each. The security
widget carries both rules: the level in force is toned by the level, so a
gate switched `off` reads as a warning whether or not anything happens to be
queued, and what is waiting for an answer is its own chip rather than a
colour borrowed by the level's. Merged, the reassuring tone would be showing
at exactly the moment nobody looks: an empty queue in front of a gate that
stops nothing.

### Panels

A panel mounts a view module into its body, and that module is the same one
whatever else it is reached from: a panel is somewhere to put a view, not a
different implementation of one. The panel names itself in its head, so the
view's own heading is hidden inside one; its lead and its actions stay,
because those are the view's rather than the panel's.

A panel is drawn before the view it holds exists, and says so. From the
moment it opens until its module has been fetched, run, and has finished
asking its endpoint it is `aria-busy`; a view that failed to arrive clears
it too, leaving the reason in the body. Without that, the empty body of a
panel still loading and the empty body of a view that had nothing to show
are the same page: a screen reader announces the dialog and reads an empty
box, and so does anything else looking at it.

A panel is dismissed by its close button, by Escape, or by going to
`#/deck`. Escape is left to the field while a field inside the panel has
focus: a key press there was never a departure, so it neither closes the
panel nor raises the question below.

### Unsaved changes

Settings, the MCP editor and the LLM route editor collect a whole form and
write it in one go, because what is being written is only coherent once its
parts agree. Until Save is pressed, everything typed lives in the page and
nowhere else. The MCP editor is why this matters: its fields are
credentials, and a saved credential is read back masked, so a change
discarded there is not an edit to make again but a secret to go and find
again.

A view says whether it is holding anything unsaved; the shell asks before
the page becomes a different one. Leaving has several doors — the close
button, Escape outside a field, the browser's back button, the widget for
another panel, the way out of Settings, the language picker — and all of
them end at the same address change, so all of them are asked once, there.
Refusing puts the address back and leaves what was typed exactly where it
was. Reloading or closing the tab is the browser's own door, so the browser
raises its own warning.

The ask is silent unless a view says it is holding something, and a view
holds something only when what is in the page differs from what is stored: a
field typed and typed back again is not a change. A warning on every panel
switch would be trained away inside a day, and then the one that mattered
would be clicked through as fast as the rest.

The conversation is the one view that keeps its own height. It scrolls its
exchange internally and holds its composer in place, so the panel around it
stops scrolling and hands the whole height over. Two nested scrollers would
mean every gesture had two possible answers and the composer would slide out
of reach, which is the exact fault that view was built to avoid.

### Addresses

`#/deck` is where the interface opens. Every panel keeps the address it had
as a page, so `#/tools` opens the deck with the tools panel over it and every
existing bookmark and cross-link still arrives somewhere sensible with the
face behind it. `#/settings` is the only address that replaces the deck.

An address that no longer names anything is followed and then replaced in
place: `#/overview` and `#/visualizer` resolve to `#/deck`, and `#/llm` to
`#/llm-routes`, so an old bookmark opens the thing that replaced it without
leaving two URLs for one state.

## LLM routes panel

`#/llm-routes` is the canonical address; `#/llm` is an alias for it, resolved
the same way every other retired address is.

The LLM routes view displays the ordered FAST, CHAT, and PRIVATE chains. Each
entry keeps active state, protocol, model, masked credential, hit and failure
counts, block time, and the last safe error label within its chain card. The
entry layout wraps long model names and error labels instead of overflowing
into neighbouring chains. The PRIVATE chain is read-only and contains one
loopback Ollama route. Configured FAST and CHAT entries are editable using
only the route schema described by the LLM spec. Named controls replace raw
JSON and preserve order and every operational field, including `api_key_env`,
`enabled`, and `capabilities`, as well as the masked direct credential. Stable
source indices let an unchanged masked key survive a rename or reorder without
exposing it. Provider-specific endpoint and model placeholders cover Ollama,
OpenAI-compatible, Claude subscription, Codex subscription, and crew routes.
The backend override and crew route selection live here rather than in general
Settings.

The API never reconstructs configured routes from runtime status. Its
`configured_routes` list is the editable disk shape; `effective_chains` is the
expanded runtime shape that also contains automatic local fallbacks. Endpoint
user-info, query values, and fragments are redacted in the response and
restored from the indexed original when the safe displayed URL is returned
unchanged. Environment-backed key values are resolved only when a backend is
built and never enter this payload.

Loading and refreshing the view reads local config and cooldown state only.
The only control that contacts a configured endpoint is **Probe models**.
Resetting cooldowns and saving routes are local file writes.

## MCP servers panel

An MCP server is a command line, an environment, and a name. All three are
edited as named controls rather than as raw JSON, the same way the LLM route
editor works and for the same reason: a text area holding a configuration
object is a way of asking someone to get the commas right.

`config.mcps` cannot ride `PUT /api/settings`. That endpoint refuses any key
the field registry does not describe, and the registry describes scalars and
lists of uniform objects; a map from a name the user invents to a launch
description with an arbitrary environment is neither. It gets its own door
rather than a special case inside someone else's, and that door carries the
same three rules: only non-default values are stored, keys this endpoint does
not own survive untouched, and a credential is writable but never readable.

The environment is where a server's credentials live, so it is edited as a
name and a value rather than as a block of `KEY=value` text: a masked value
has to survive being displayed and saved untouched, and in a text block the
mask would be indistinguishable from someone having typed eight bullets.
Values are masked to their last four, and a mask returned unchanged leaves
the stored secret alone. A stable `_index` on each entry is what makes that
survive a rename, so renaming a server is an edit rather than a new server
whose credentials were left behind under the old name.

Everything is validated before anything is written. A refusal half way down
the list would otherwise leave the file describing a set of servers nobody
asked for.

Two facts sit side by side on every card and are never merged. What is
*configured* is what this panel writes; what is *connected* is what the
running daemon actually managed to launch and ask. A server saved a moment
ago is configured and not connected, and the panel says so: tools are
discovered when the daemon starts, so a new server is on disk now and
reachable after a restart. Before any discovery pass has run, "not connected"
would be a guess rather than a reading, and the panel says that instead.

## Today panel

The assistant already has a morning briefing: once per local day, at a
configured time, it reads the School branch and speaks the result. That is
the right shape for something that should find you without being asked, and
the wrong shape for something you want to check. Speech has happened or it
has not, it cannot be re-read, and before the trigger time it does not exist.

This is the same question put to the same source. It shares the branch reader
and the generator with `../memory/morning_briefing.py` deliberately: two
briefings phrased by two prompts would eventually disagree about the same
day, and the one you could not re-read would be the one you half remembered.

What differs is only what a screen can do that a speaker cannot:

| Reading | Cost | When |
|---|---|---|
| The items | A bounded graph read, no model | Every request |
| The prose | One CHAT-tier call | Only when asked, then cached for the local day |

The split is the whole design. A widget on the deck repaints every ten
seconds; a briefing that generated prose on every repaint would run a
CHAT-tier model six times a minute for a card three lines tall.

The cached prose is kept in `app_state` under its own key, deliberately
separate from the spoken briefing's own gate, because reading a briefing on
screen must never persuade the spoken one that it has already delivered
today. A summary written yesterday is not shown and not deleted: the next
refresh overwrites it, and until then it is simply not today's.

Nothing here invents a school. An empty branch reads as empty and is never
sent to a model; a generation that fails says so.

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

## The face

A face that idles, listens, thinks, and speaks in step with the real
conversation. It is one circle inside one ring, drawn by `static/js/face.js`
into a canvas in the page.

The face is the centre of the deck and is mounted once for as long as the
page is open. Opening a panel does not rebuild it: a face rebuilt on every
navigation would restart its animation and blink at the reader each time they
looked at a different reading.

### What it draws

The disc inside the ring is the reading, and how much of the ring it fills is
what the state is. That matters more than it sounds: it is a channel that
survives a reader who has asked for no motion, and with every animation off
the four states are still four different pictures. A face that separated
`listening` from `thinking` by the speed of a rotation would have nothing
left to say to that reader, and reporting the state is the only reason this
drawing exists.

| Reading | The face shows |
|---|---|
| `idle` | The disc at rest, at just over half the ring, breathing slowly |
| `listening` | The disc open to four fifths of the ring: the largest step on the scale, and the one that has to read across a room |
| `thinking` | The disc back at rest with one mark travelling round the ring. One moving part rather than an orbit of them, parked at the top when nothing may move |
| `speaking` | The disc at two thirds, its edge pushed by the block of audio the TTS engine last wrote to the speakers |

A waveform may move the edge of the disc by at most a fourteenth of its
radius. Past roughly a tenth an outline stops reading as a circle that is
speaking and starts reading as a shape that is not a circle, and the samples
arrive raw from the speakers, so they are normalised against their own peak
and smoothed against their neighbours before they are drawn.

Everything is painted from `var(--accent)`, read off the stylesheet rather
than held in the module, so a theme drives the face for free and there is no
second palette to keep in step with the first. The custom property is
resolved when the theme changes rather than on every frame.

`motionAllowed()` decides whether there is an animation loop at all. When the
answer is no there is no loop: a new reading is the only thing that repaints,
so the picture is genuinely still rather than animating slowly.

### Dressing it

How large the face is drawn is a control beside it rather than in Settings.
How the assistant looks while it is talking to you is a different kind of
decision from which port the daemon binds, and it is made while looking at
the thing it changes. The size is this browser's, in `localStorage`, the same
way the theme is. There is nothing else to choose: there is one face, and it
is ours.

### Where the reading comes from

The face polls `/api/visualizer/state` roughly eight times a second, answered
by `jarvis.webui.visualizer.state`, which derives the reading entirely from
Jarvis's own live objects:

| Reading | Source |
|---|---|
| `state` | The runtime phase (`jarvis.runtime.state.Phase`), mapped to `idle`, `listening`, `thinking`, or `speaking`. `capturing` reads as listening; `transcribing`, `thinking`, and `tool` all read as thinking; `starting` and `dictating` read as idle |
| `level`, `samples` | The most recent block of audio a TTS engine wrote to the speakers, fed in by `PiperTTS` and `KokoroTTS` as they play. A waveform older than 0.6 seconds is stale and is not shown; when the samples are fresh they are trusted as speech even if the phase reading has not caught up yet |
| `alert`, `loading` | Always `false`. Jarvis has no attention-signal concept and no TTS engine plays a thinking sound separate from the reply itself, so nothing here would ever set them |

No signal files are read or written, and no second HTTP server runs: one
process, the same principle every other view in this spec follows.

A hidden page stops asking rather than merely stopping drawing. The server
closes every connection it answers, so a poll is a new socket rather than a
reuse of one and the operating system holds each closed socket for minutes
afterwards. Eight a second is affordable while someone is watching the face
and is not affordable for a tab left behind another window all day. Returning
to the page takes a reading at once rather than waiting out the skipped tick.

What the face draws is the assistant's own reading; the words beside it come
from the event stream instead, because that is the only source that can say
this page has lost the daemon. A poll that fails and an assistant that is
idle look identical from here, so a failed poll holds the last honest reading
rather than dropping the face to idle.

## Passive record

While passive capture is on, the header carries a recording indicator beside
the phase on every view, so a page open at any depth still says whether the
room is being written down. It is driven by the `passive` event rather than
polling, and shows nothing at all while the switch is off.

The record has a panel of its own rather than a section inside another one.
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
