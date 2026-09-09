# LLM Backend Specification

The `jarvis.llm` package owns every LLM completion call. Jarvis mainly speaks generic, self-hostable protocols: native Ollama and OpenAI-compatible HTTP. Route configuration contains protocol names, URLs, credentials, model names, tiers, and timeouts. Three named exceptions carry different shapes: the `claude_subscription` provider reaches `claude_agent_sdk` in an isolated subprocess authenticated against Felix's own Claude Code CLI login rather than a metered API key (see "Claude subscription session" below), the `codex_subscription` provider runs the authenticated Codex CLI against Felix's ChatGPT subscription (see "Codex subscription session" below), and the `crew_chat` provider is plain vendor-neutral HTTP but reads its endpoint and credential from the existing Mission Control fields (`cfg.crew_api_url` / `cfg.crew_api_key`) rather than its own route entry (see "Crew chat relay" below). Every other provider stays vendor-neutral HTTP with no SDK and no config indirection.

The control centre treats the stored list and the running chains as different
objects. `configured_routes` is the ordered, schema-complete disk shape and is
the only input to its editor. `effective_chains` is read-only runtime status.
FAST and CHAT contain only explicit cloud/subscription routes; PRIVATE contains
the one loopback Ollama route. Direct keys are masked and environment key
values are never loaded by the configuration endpoint.

## Goals

1. **Honest cloud lanes.** An empty FAST or CHAT chain stays empty and never silently sends that work to Ollama.
2. **One dispatch path.** `get_llm_backend(cfg)` always returns `RoutedBackend`, including a purely local configuration.
3. **Explicit privacy lane.** Memory writes and graph rewrites use `Tier.PRIVATE`, which contains one loopback `OllamaBackend` route. With route chains configured, embeddings also use loopback Ollama and never enter the router.
4. **Fail-soft at the boundary.** Concrete OpenAI-compatible backends raise typed provider failures. `RoutedBackend` turns an exhausted chain into `None`, preserving the contract used by callers.
5. **No secret disclosure.** URLs, credentials, response bodies, and exception messages are absent from error logs. Route credentials are masked in the control centre.

## Public surface

```python
from jarvis.llm import (
    LLMBackend,
    OllamaBackend,
    OpenAICompatibleBackend,
    ClaudeSubscriptionBackend,
    CodexSubscriptionBackend,
    CrewChatBackend,
    RoutedBackend,
    RequestDeadline,
    Route,
    Tier,
    ProviderError,
    RateLimitedError,
    QuotaExhaustedError,
    AuthError,
    ModelUnavailableError,
    ToolsNotSupportedError,
    get_llm_backend,
    get_embedding_backend,
    resolve_model,
)
```

Function-style Ollama helpers remain available to performance tests and eval scripts that only have a base URL. Production modules construct backends through the factories.

## Backend interface

| Method | Return | Contract |
|---|---|---|
| `direct(model, system, user, ...)` | `Optional[str]` | One system and user completion. |
| `streaming(model, system, user, on_token, ...)` | `Optional[str]` | Streams text chunks and returns their concatenation. |
| `chat(model, messages, ...)` | `Optional[dict]` | Arbitrary messages, including native tool schemas. |
| `embed(text, model, ...)` | `Optional[list[float]]` | Vector embedding. Only concrete embedding backends are used. |
| `list_models(...)` | `list[str]` | Models exposed by an endpoint. |
| `warm_up(model, ...)` | `bool` | Best-effort model reachability and loading probe. |

`chat()` accepts `on_token`, which asks for the assistant's text as it arrives rather than only at the end so a caller can start speaking the first sentence while the rest is still being written. It changes when the text shows up, not what comes back: the return value is the same assembled response either way, tool calls survive the fold (reassembled by index on the OpenAI shape, where they are split across deltas), and reasoning is collected but never reported through it. A listener that raises is logged and ignored, because reporting text is a side effect and must not cost the caller its reply. `RoutedBackend` passes the listener only to routes that declare the `stream` capability, so falling through the chain never depends on whether the caller wanted its text early.

`direct()` and `streaming()` are convenience shapes over chat completions. Messages are stripped to fields allowed by the OpenAI Chat Completions schema before transmission.

### Tool calling

`ToolsNotSupportedError` means the selected model rejected a supplied native tool schema. It is not a routing signal. `RoutedBackend` passes it to the reply engine, which changes to text-based tool calls without losing the turn. `ClaudeSubscriptionBackend` and `CodexSubscriptionBackend` raise it unconditionally whenever `tools` is supplied, before a subprocess starts: neither subscription session accepts Jarvis's native tool schemas (see "Claude subscription session" and "Codex subscription session" below). `CrewChatBackend` raises it the same way, before any request is made: the crew's chat endpoint has no tool-calling concept of its own (see "Crew chat relay" below).

### Streaming

`RequestDeadline` carries one monotonic budget across route attempts. When no explicit deadline is supplied, `RoutedBackend.streaming()` derives it from the caller timeout. Routes are attempted in configured order; there is no speculative local-first worker or progress-window failure classification.

The first route to emit meaningful text owns the answer. Once a route owns the answer, a later stream failure is recorded as a stream abort and no second route is allowed to splice another answer onto it.

## Typed provider failures

`OpenAICompatibleBackend` raises:

| Condition | Exception | Metadata |
|---|---|---|
| HTTP 429 rate limit | `RateLimitedError` | `retry_after`, parsed from `Retry-After` or generic rate-limit reset headers |
| HTTP 429 quota exhaustion | `QuotaExhaustedError` | `reset_at`, when the endpoint states one |
| HTTP 401 or 403 | `AuthError` | none |
| HTTP 404 | `ModelUnavailableError` | none |
| Timeout or other request failure | `ProviderError` | none |

Exception text is generic and contains no endpoint URL, key, response body, or model name. `ToolsNotSupportedError` remains separate.

## Routing

`Route` is a frozen dataclass with `name`, `provider`, `base_url`, `api_key`, `api_key_env`, `model`, `tier`, `timeout_sec`, `enabled`, `capabilities`, and `keep_alive`. Its direct credential field is excluded from `repr`; an environment credential is resolved only while constructing its backend.

`RoutedBackend` groups routes by tier and tries each enabled, capable, unblocked route in configuration order. Streaming and CHAT calls carry one `RequestDeadline` across route attempts. A route's timeout is the smaller of its own limit and the remaining caller budget. A provider failure, connection failure, timeout, model failure, auth failure, or empty response moves to the next candidate. An exhausted non-streaming chain returns `None`; the reply layer renders its fixed failure message.

Configured `codex_subscription`, `claude_subscription`, and `crew_chat` routes are accepted only for CHAT. FAST and PRIVATE entries for these providers are dropped before a backend is built.

Configured FAST and CHAT chains contain only enabled, credentialed cloud or subscription routes. They never append Ollama, loopback, LAN, or an implicit single-endpoint fallback. A configuration with no valid route therefore has no effective candidate in that lane. `resolve_model()` returns a string-compatible value carrying its `Tier`, so existing backend method signatures remain ordinary model-string APIs while the router can select a chain.

A route the user switched off remains visible in `configured_routes` but is inert. Blank environment variables count as missing credentials, so those routes are not imported into an effective chain and are not attempted.

### Model residency

Ollama unloads a model once its keep-alive lapses and resets that timer from each request's own `keep_alive`, applying its short default when the field is absent. Warming a model once is therefore not enough: an assistant that idles between conversations pays a cold page-in on the next thing the user says. Every route built against an Ollama runtime carries a `keep_alive`, and `OllamaBackend` stamps it onto each `direct`, `streaming`, and `chat` request unless the caller passed one explicitly. The duration is `30m`, or `1m` under `low_power_mode`, which trades warmth for handing the GPU back between turns. Remote OpenAI-compatible routes carry no residency: it is an Ollama knob and their servers own the decision.

`warm_up()` warms only the first available candidate for the requested lane. `list_models()` combines unique names from reachable routes.

### Cooldown state

`RouteStateStore` writes `~/.jarvis/llm_routes_state.json` atomically with mode `0o600` where the platform supports POSIX permissions. The file contains route hashes and health counters, never URLs, models, or credentials.

| Failure | Block |
|---|---|
| 429 with `Retry-After` | Exactly the stated duration |
| 429 without a duration | 60 seconds, then 300 seconds, then 900 seconds |
| Quota exhaustion with reset | Until the stated reset |
| Quota exhaustion without reset | Until midnight UTC |
| 401 or 403 | Invalid for the process lifetime |

Persisted cooldowns prevent a restart from immediately touching a rate-limited or quota-exhausted key. Authentication invalidation is deliberately process-local, so a restarted process can retry a corrected external credential.

Route-state format v2 keys each route by a hash of tier, provider, base URL,
and model. Counters distinguish started attempts, successes, provider
failures, empty replies, cooldown skips, deadline-before-attempt skips,
stream aborts, and whole-chain exhaustion. Only a provider call that actually
started may increment an attempt or provider-failure counter. Status separately
names configured, selectable, next-selectable, and last-responding routes and
tracks safe failure labels plus attempt/success/failure timestamps. Reset may
target one stable route id or the whole store; v1 files are read tolerantly.

### Runtime generations and capability probes

`LLMRuntime` owns an immutable settings/backend generation. A turn obtains one
snapshot and keeps it to completion. Route PUT writes and reloads the candidate,
constructs all adapters, then publishes the new generation atomically; any
failure restores the previous file and generation. The voice daemon,
conversation API, and direct reply path all acquire snapshots from this same
runtime. Unchanged adapters are reused and all unique subscription sidecars are
closed at daemon shutdown.

`POST /api/llm/routes/probe` explicitly probes every configured non-PRIVATE
route, including disabled or credential-missing candidates. Results contain
only capability booleans, counts, model ids, timings, and stable error classes
(`auth`, `billing`, `quota`, `model_missing`, `timeout`, `transport`, or
`empty_response`). Prompts, generated text, bodies, URLs with credentials, and
credential values are never retained or returned. Subscription and crew routes
use completion probes; native tool support and the text-tool fallback are
reported separately.

`describe_model_topology(cfg)` is the status boundary for model names. It
reports the first currently available candidate in each effective tier with
its provider and loopback-derived `local`/`remote` location, separately from
the configured Ollama PRIVATE and embedding roles. It never claims residency;
only the independent `ollama ps` system
reading can say which weights are actually loaded.

## Lanes

| Tier | Chain | Contexts |
|---|---|---|
| `Tier.FAST` | Explicit cloud routes only | intent judge, tool router, tool search, enrichment extractor, evaluator, weather place extraction, school exam extraction |
| `Tier.CHAT` | Explicit cloud/subscription routes only | reply loop, planner, step resolver, dictation cleanup, nutrition calls, spoken school morning briefing, other tool-specific completions |
| `Tier.PRIVATE` | loopback Ollama only | memory/tool/loop summaries, diary summary, deflection rewrite, topic optimisation, graph extraction, node merge, graph auto-split, school-note import |

Memory retrieval may send the selected snippet text into FAST or CHAT calls.
The added provenance fields stay attached to local Python objects and do not
enter those prompts by default. Diary text retains its existing date prefix for
recency handling; graph node ids and branches, vault paths, and Remio titles
enter a CHAT tool-result message only after the user asks for the source and the
model invokes `memoryProvenance`. Memory creation, graph mutation, and
embeddings stay local.

## Chat backend selection

The main reply loop's Tier.CHAT call (`chat_with_messages` in `src/jarvis/reply/engine.py`) can bias which configured route answers a given turn, on top of the ordinary chain fallback above. This is a per-call hint, not a second routing mechanism: `RoutedBackend.chat(preferred_provider=...)` only reorders its existing candidate list for that one call, promoting routes of the named provider to the front while leaving the rest of the chain reachable immediately after. A promoted route that is missing from the chain, or present but failing, falls through to the normal chain order exactly as an unpromoted failure would — this feature can never leave a turn with no answer that the existing chain would have produced.

Two independent sources feed `preferred_provider`, resolved in `chat_with_messages` via `_resolve_preferred_chat_provider`:

- **Manual override** — `cfg.chat_backend_override`. `"auto"` (the default) defers to automatic classification. Any other accepted value names a configured cloud/subscription provider to try first. `ollama` is rejected; migration v7 rewrites it to `auto`.
- **Automatic classification** — only consulted under `"auto"`. The tool router emits `DEFAULT`, `COMPLEX`, or `HERMES` in the same response that picks the allow-list. `DEFAULT` leaves configured order unchanged, `COMPLEX` prefers `claude_subscription`, and `HERMES` prefers `crew_chat`. Legacy `LOCAL` parses as `DEFAULT`; it never selects a local route.

The router's classification travels from the tool-router call site to the chat call within one reply exactly like `routed_tools` does: computed once, reused for every turn of that reply's agentic loop, and carried through a hot-window cache hit alongside the cached tool list so a repeated query does not lose it.

`debug_log` fires when the manual override forces a provider, when automatic classification selects `claude_subscription` or `crew_chat`, and when a preferred provider has no matching route and the call falls through to the normal chain order.

## Embeddings

`get_embedding_backend(cfg)` always returns an `OllamaBackend` whose URL is loopback. It never returns `RoutedBackend`, and the model remains `cfg.ollama_embed_model`, preserving the vector space used by stored embeddings. A configured non-loopback Ollama URL is replaced by `http://127.0.0.1:11434` for PRIVATE and embedding work.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `llm_routes` | `[]` | Ordered generic endpoint entries for FAST and CHAT |
| `chat_backend_override` | `"auto"` | `"auto"` or a route provider name to force for every Tier.CHAT reply; see "Chat backend selection" |
| `llm_provider` | `"ollama"` | Legacy single-endpoint value; never creates FAST/CHAT routes |
| `llm_base_url` | `""` | Legacy single-endpoint URL; v7 migrates eligible public cloud routes |
| `llm_api_key` | `""` | Legacy credential used only by that one-time migration |
| `llm_chat_model` | local model | Effective first CHAT model |
| `fast_model` | automatic | Effective first FAST route model (derived at load time) |
| `local_fast_model` | removed | Removed by migration v7 |
| `ollama_base_url` | `http://127.0.0.1:11434` | PRIVATE and embedding runtime; forced to loopback |
| `ollama_chat_model` | setup selection | PRIVATE model only |
| `ollama_embed_model` | `nomic-embed-text` | Local embedding model |

Each `llm_routes` entry has this shape:

```json
{
  "name": "descriptive-name",
  "provider": "openai_compatible",
  "base_url": "https://endpoint.example/v1",
  "api_key": "",
  "api_key_env": "PROVIDER_API_KEY",
  "model": "model-exposed-by-the-endpoint",
  "tier": "chat",
  "timeout_sec": 4.0,
  "enabled": true,
  "capabilities": ["chat", "stream", "tools"]
}
```

The loader accepts this tiered shape, including every provider described
below, and ignores malformed entries. List order is route order. A credential
may be stored directly in `api_key` or referenced by `api_key_env`; environment
values are resolved only when the backend is built and are never copied into
configuration. The control-centre editor accepts the same provider set and
round-trips `api_key_env`, `enabled`, and `capabilities` together with the
masked direct credential.

The data-only FCC endpoint catalogue contains ordinary OpenAI-compatible templates for Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`, `GEMINI_API_KEY`, `FCC_SMOKE_MODEL_GEMINI`) and OpenRouter (`https://openrouter.ai/api/v1`, `OPENROUTER_API_KEY`, `FCC_SMOKE_MODEL_OPEN_ROUTER`). Both templates participate in the CHAT import order only. Neither participates in FAST because the catalogue fixes no low-latency model configuration, and OpenRouter adds broker and upstream variability. A catalogue template is not an active route: the importer requires a configured credential and a model returned by that endpoint's live `GET /models` response before writing a route.

`provider` may also be `"claude_subscription"` (see "Claude subscription session" below), `"codex_subscription"` (see "Codex subscription session" below), or `"crew_chat"` (see "Crew chat relay" below). None carries a real route credential. `claude_subscription` never dials its `base_url`, so a non-empty placeholder such as `"claude-agent-sdk"` satisfies the shape check. `crew_chat` reuses `cfg.crew_api_url`/`cfg.crew_api_key`/`cfg.crew_chat_agent`, so its `base_url`/`api_key`/`model` fields are placeholders such as `"crew-chat"`. A `codex_subscription` route uses `base_url: "codex-cli"` as its non-empty placeholder and interprets `model` as the Codex model name passed to `--model`; it never reads `api_key` or `api_key_env`. Its reasoning effort is the backend's latency-oriented `low` setting. All three are route-chain providers only and are absent from the single-endpoint `llm_provider` and `embedding_provider` choices. The subscription routes are CHAT-only, and every configured route resolving to `Tier.PRIVATE` is dropped before a backend is built.

A Codex subscription route has this shape:

```json
{
  "name": "codex-subscription",
  "provider": "codex_subscription",
  "base_url": "codex-cli",
  "api_key": "",
  "model": "gpt-5.6-sol",
  "tier": "chat",
  "timeout_sec": 60.0,
  "enabled": true,
  "capabilities": ["chat", "stream", "tools"]
}
```

Config migration version 5 converts priority-based route lists into ordered FAST and CHAT entries. It preserves activation, capabilities, and environment-variable names without reading their values. Existing tiered entries receive the same explicit defaults, and repeated migration is idempotent.

`scripts/import_fcc_keys.py` probes keys from `~/.fcc/.env` and writes routes only for endpoints that advertise a model. `python -m jarvis.llm.probe` performs `GET /models`, prints no credential material, and stores the observed catalogues in `~/.jarvis/llm_probe.json` with mode `0o600`. Model names come from live endpoint responses or a probed FCC smoke model that is present in that response.

## Wire shapes

### Ollama

- `POST /api/chat`, `POST /api/embeddings`, `GET /api/tags`, `GET /api/version`
- JSON-lines streaming
- OpenAI-compatible tool schemas
- `cache_prompt: true` on chat payloads
- `max_tokens` translated to `num_predict`

### OpenAI-compatible

- `POST /chat/completions`, `POST /embeddings`, `GET /models`
- Server-Sent Events streaming
- Tool-call argument strings decoded to dictionaries on responses and encoded on subsequent requests
- Ollama-only request options omitted
- Optional `Authorization: Bearer` header

### Claude subscription session

`ClaudeSubscriptionBackend` talks to a local sidecar subprocess over newline-delimited JSON on dedicated stdin/stdout pipes. The sidecar is the only module that imports `claude_agent_sdk`; it authenticates through `ClaudeSDKClient`, which spawns the `claude` CLI and inherits Felix's existing subscription session. The client removes `ANTHROPIC_API_KEY` from the child environment before launch. No metered API key is read, set, or stored for this route; there is no key to mask.

The client sends `{"cmd":"generate","id":N,"model":"...","system_prompt":"...","prompt":"...","stream":true|false}` or `{"cmd":"shutdown"}`. The sidecar sends `ready` once, zero or more request-scoped `chunk` and `tool_denied` events, then either `{"type":"result","id":N,"text":"..."}` or `{"type":"error","id":N,"status":401|403|404|429|null}`. All text pipes use UTF-8, requests are serial, and no network socket is opened.

- `jarvis.llm.claude_subscription_sidecar_client` is the main-process side of the boundary. It launches lazily on first generation, serialises one request at a time, streams text chunks when requested, and reuses the running sidecar. It never imports `claude_agent_sdk`. `jarvis.llm.claude_subscription_sidecar` is the standalone entry point and the only SDK importer. A source-scan test enforces the boundary.
- The dedicated environment lives at `~/.jarvis/claude-subscription-venv`. From a Windows source checkout, create or refresh it with `.venv\Scripts\python.exe scripts\setup_claude_subscription.py`. On macOS or Linux, use `.venv/bin/python scripts/setup_claude_subscription.py`. The bootstrap installs `requirements-claude-sidecar.txt` into that environment only. `JARVIS_CLAUDE_SIDECAR_PYTHON` may name a different sidecar interpreter; the client never shells out to a hardcoded absolute path.
- The environment is optional. Construction and daemon startup launch nothing. A route first used without a valid sidecar interpreter, without the SDK, or without a usable `claude` CLI raises a typed `ProviderError`; `RoutedBackend` treats it as an ordinary route failure and continues to the next candidate.
- The sidecar process reports `ready` after importing the SDK, then serves generation requests until `shutdown` or stdin EOF. A generation failure produces an error response and leaves the process available for later calls. A crash, broken pipe, invalid response, readiness timeout, or request timeout drops the process so the next call may launch a clean one.
- Every generation opens a fresh `ClaudeSDKClient`, sends one prompt, and disconnects. Jarvis's own `LLMBackend` contract already carries the full conversation on every call (`chat()` receives the whole `messages` list; `direct()` receives system and user text together), so resuming or continuing an SDK session across calls would duplicate that context rather than save anything. `chat()`'s multi-role `messages` list is flattened into one system prompt plus one labelled transcript string, because `ClaudeSDKClient.query()` takes a single prompt per call.
- The session is stripped to text generation only inside the sidecar, because `ClaudeSDKClient` is otherwise a fully agentic session with its own tool-calling loop and Jarvis owns exactly one tool-calling loop and one security gate (`../security/security.spec.md`). Every session sets `tools=[]`, `setting_sources=[]`, and `mcp_servers={}`, and always passes a `can_use_tool` callback that denies every attempt unconditionally. The empty tool/settings/MCP options are not sufficient alone: an authenticated session can still see MCP tools attached at the Anthropic account level (connectors configured in the Claude.ai account the CLI is logged into), entirely outside this process's control, and the model can still attempt to call one. The `can_use_tool` deny-all callback is the mechanism that actually stops that attempt, and is mandatory rather than an alternative to the empty tool list. `permission_mode` is always `"default"`; a mode that auto-approves calls ahead of `can_use_tool` (e.g. `bypassPermissions`) would silently defeat the deny-all gate, per the SDK's own `CanUseToolShadowedWarning`.
- Every denied tool-use attempt is emitted as a protocol event and recorded through main-process `debug_log`; every backend selection, sidecar launch, readiness transition, and sanitised failure class is also recorded. Tool input, prompt text, paths, credentials, SDK exception text, and stderr never enter a log line or public exception.
- No native tool schema is ever satisfiable (see "Tool calling" above): `chat()` raises `ToolsNotSupportedError` whenever `tools` is supplied, before any session is opened.
- No sampling controls: `num_ctx`, `thinking`, `temperature`, and `max_tokens` have no equivalent exposed by `ClaudeAgentOptions` for a single generation call, so they are accepted for signature parity and silently ignored, the same way `OpenAICompatibleBackend` ignores Ollama-only knobs it cannot express.
- No model-listing or warm-up endpoint: `list_models()` returns `[]` and `warm_up()` is a no-op returning `True`; nothing needs paging in and nothing is worth a real round trip at every daemon start.
- A failed `ResultMessage` (`is_error=True`) or raised SDK failure becomes a status-only sidecar error response. Status 401/403 maps to `AuthError`, 404 to `ModelUnavailableError`, 429 to `RateLimitedError`, and everything else (including a missing `claude` CLI) maps to `ProviderError`. Nothing but these typed exceptions or an assembled string leaves the backend.
- `claude_agent_sdk` is absent from `requirements.txt`: it requires `mcp>=1.23.0,<3.0.0`, which conflicts with the `mcp==1.13.1` pin the persistent MCP runtime depends on (`../tools/external/mcp_runtime.spec.md`). `requirements-claude-sidecar.txt` belongs only in the dedicated sidecar environment.

### Codex subscription session

`CodexSubscriptionBackend` starts `codex exec` for each generation and authenticates through the Codex CLI's existing ChatGPT login. It does not read, set, or store an OpenAI API key. Direct access-token variables and every inherited `*_API_KEY` variable are removed from the child environment while `CODEX_HOME` remains available for the subscription login. The configured route model is passed to `--model`; every invocation fixes `model_reasoning_effort="low"` to limit latency.

- The prompt is written to stdin with `-`, never placed in the process argument vector. `chat()` uses the same `_flatten_messages()` shape as `ClaudeSubscriptionBackend`: system-role content becomes the prompt head and all other roles form a labelled transcript.
- Every child command explicitly passes `--sandbox read-only`, `-c approval_policy="never"`, `-c forced_login_method="chatgpt"`, `-c web_search="disabled"`, `-c features.shell_tool=false`, `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, `--skip-git-repo-check`, and `--color never`. `read-only` is the most restrictive sandbox exposed by `codex exec`. `approval_policy="never"` prevents an unattended voice turn from waiting for interactive approval while the read-only sandbox remains enforced. Web search and the default shell tool are disabled, leaving only model text generation. Ignoring user configuration prevents global `danger-full-access`, MCP, hook, and other agent settings from entering the answer path; authentication still uses `CODEX_HOME`, while the forced login method prevents a direct API credential from becoming the active provider.
- Each request creates a fresh empty temporary directory and supplies it both as the subprocess `cwd` and through `--cd`. The CLI cannot inherit the daemon's launch directory or discover Jarvis's repository as its workspace. The directory is removed after the reaped child exits.
- `codex exec --json` supplies JSONL events. Only completed `agent_message` text is assembled. Each completed message is forwarded to `on_token` as it arrives; the CLI exposes whole-message events rather than token deltas. Blank assembled text is an empty response, so routing continues.
- `chat()` raises `ToolsNotSupportedError` whenever `tools` is supplied, before the process starts. `direct()` and `streaming()` are convenience shapes over the same invocation. `embed()` returns `None`, `list_models()` returns `[]`, and `warm_up()` returns `True` without a process.
- The subprocess receives the smaller of the route timeout and the remaining caller `RequestDeadline`. When that budget expires, the process group is interrupted or killed and the child is waited for before `ProviderError("provider request timed out")` is raised.
- Authentication or an expired login maps to `AuthError`, an unknown model to `ModelUnavailableError`, rate limiting to `RateLimitedError`, quota refusal to `QuotaExhaustedError`, and every other failure, including a missing `codex` executable, to `ProviderError`. Logs and exceptions contain only the backend decision and typed failure class. Prompt text, paths, stderr, response bodies, and model names are excluded.

### Crew chat relay

`CrewChatBackend` relays Tier.CHAT turns to the Hermes crew's own chat engine on Felix's NAS, over the same wire shape already working in `jarvis.webui.api.crew`'s `crew_chat()` (Mission Control's own web-UI chat feature, which stays exactly as it is): `POST {crew_api_url}/chat` with `{"agent": ..., "message": ...}` and an `X-Crew-Key` header when `cfg.crew_api_key` is set, relaying back whatever the NAS-side endpoint proxies from the crew's own chat engine. `chat()`'s multi-role `messages` list is flattened into one message string the same way `ClaudeSubscriptionBackend.chat()` flattens for `ClaudeSDKClient.query()`: a system prompt plus a labelled transcript, joined here into the single field the crew endpoint expects rather than sent separately.

`cfg.crew_chat_agent` names which crew specialist answers, from the same fixed roster `askCrew` delegates to. This is a wholly independent path from `askCrew` (`../tools/builtin/ask_crew.spec.md`): `askCrew` stays fire-and-forget over Telegram, and `CrewChatBackend` is a synchronous `LLMBackend` that happens to talk to the same NAS endpoint family. Neither `crew_api_url` nor `crew_chat_agent` being set is a route error the loader rejects; an empty endpoint or agent instead fails closed at request time with a typed `ProviderError`, exactly the same way an empty `askCrew` configuration refuses rather than guessing a channel. `RoutedBackend` treats that `ProviderError` as an ordinary route failure and falls through the rest of the chain, so a half-configured `crew_chat` route can never leave a turn unanswered.

Text generation only, the same posture as `ClaudeSubscriptionBackend`: the crew's chat endpoint has no tool-calling shape of its own, so `chat()` raises `ToolsNotSupportedError` whenever `tools` is supplied, before any request is made. `direct()` and `streaming()` are convenience shapes over the same single HTTP call; `streaming()`'s `on_token` fires once with the whole reply rather than per-token, because the endpoint has no incremental shape to forward. `embed()` always returns `None` and `list_models()` always returns `[]`, the same posture as every backend without those concepts. `warm_up()` is a no-op returning `True`: nothing needs paging in over HTTP.

Typed failures follow the "Typed provider failures" table above, reusing `OpenAICompatibleBackend`'s own status-code mapping rather than reimplementing it: HTTP 401/403 to `AuthError`, 404 to `ModelUnavailableError`, 429 to `RateLimitedError` or `QuotaExhaustedError` depending on the response, anything else to `ProviderError`. A connection failure or timeout is also a `ProviderError`. A missing or blank `reply` field in an otherwise successful response is an empty response, not an exception, so `RoutedBackend` moves on to the next candidate exactly as it would for any backend that produced nothing. No endpoint URL, credential, or response body ever reaches an exception message or a log line.

## File layout

```text
src/jarvis/llm/
├── backend.py
├── claude_subscription.py
├── codex_subscription.py
├── crew_chat.py
├── factory.py
├── ollama.py
├── openai_compatible.py
├── route.py
├── route_state.py
├── route_catalogue.py
├── probe.py
├── tiers.py
└── llm.spec.md
```

Runtime generations own copied settings. Resolved environment credentials participate
in the process-salted generation fingerprint; rotation builds a fresh adapter while
in-flight turns retain their existing adapter. Crew credential changes rebuild
adapters. A failed build closes only newly created adapters and keeps the active
generation usable.

Groq GPT-OSS completion requests use `reasoning_effort=low` by default and
`max_completion_tokens` with at least 1024 tokens, shared by reasoning and visible
output. Direct, streaming and tool requests share this policy. Explicit larger
budgets and reasoning effort are preserved. Other endpoints keep their payloads.
API contract: https://console.groq.com/docs/api-reference

Graph placement and school-note imports always use PRIVATE, including category classification.

Atomic config persistence retries Windows sharing/lock violations up to three
times with bounded backoff. Other permission errors fail immediately; an
unsuccessful replacement preserves the original config and removes the temp file.
