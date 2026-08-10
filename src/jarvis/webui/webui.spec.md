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

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `webui_enabled` | bool | `true` | Serve the control centre with the daemon |
| `webui_port` | int | `5055` | Listening port. Anything outside 1024-65535 falls back to the default rather than failing the start |
| `webui_bind_host` | str | `"127.0.0.1"` | `0.0.0.0` reaches it from the same network and turns the token on |
| `webui_token` | str | `""` | Empty mints one per start when off loopback |
| `webui_open_browser` | bool | `false` | Open the interface at daemon start |
