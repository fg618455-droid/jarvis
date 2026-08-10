# Control Centre Specification

The control centre is the local web interface for the whole assistant: live
state, memory, conversation, tools, security, technical readings, and
settings. The daemon serves it in-process.

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
| `GET /api/events` | Server-sent events. Opens with the current state so a page that connects mid session is correct at once |
| `GET /api/turns` | Recent turns with their stages and tool calls |
| `GET /api/turns/export.csv` | The same history flattened, one column per stage |
| `GET /api/conversation` | Recent turns plus the discarded-utterance counts |
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
| `GET/PUT /api/settings` | Every editable config field, and writes to it |

`POST /api/chat` runs one turn at a time. A second request while a reply is
being written is refused with 409 rather than queued: the reply engine is
not built for concurrent turns against one dialogue memory, and a person
typing cannot outrun it. With the daemon running, a typed turn joins the
spoken conversation; standalone, the control centre keeps one of its own.

`PUT /api/settings` follows the same two rules as the Qt settings window,
because both write the same file: only non-default values are stored, and
keys the registry does not describe survive untouched. A credential is sent
back masked, and a masked value returned unchanged leaves the stored one
alone, so saving a form never overwrites a secret with its own mask.

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

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `webui_enabled` | bool | `true` | Serve the control centre with the daemon |
| `webui_port` | int | `5055` | Listening port. Anything outside 1024-65535 falls back to the default rather than failing the start |
| `webui_bind_host` | str | `"127.0.0.1"` | `0.0.0.0` reaches it from the same network and turns the token on |
| `webui_token` | str | `""` | Empty mints one per start when off loopback |
| `webui_open_browser` | bool | `false` | Open the interface at daemon start |
