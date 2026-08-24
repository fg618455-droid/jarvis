# LLM Backend Specification

The `jarvis.llm` package owns every LLM completion call. Jarvis mainly speaks generic, self-hostable protocols: native Ollama and OpenAI-compatible HTTP. Route configuration contains protocol names, URLs, credentials, model names, tiers, and timeouts. The one named exception is the `claude_subscription` provider, which rides `claude_agent_sdk` against Felix's own Claude Code CLI login rather than a metered API key (see "Claude subscription session" below); every other provider stays vendor-neutral HTTP with no SDK.

## Goals

1. **Offline by default.** An empty `llm_routes` list uses Ollama exactly as a local installation expects.
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

`ToolsNotSupportedError` means the selected model rejected a supplied native tool schema. It is not a routing signal. `RoutedBackend` passes it to the reply engine, which changes to text-based tool calls without losing the turn. `ClaudeSubscriptionBackend` raises it unconditionally whenever `tools` is supplied, never only on a transient rejection: its session never carries a usable tool (see "Claude subscription session" below), so a native tool schema can never be satisfied.

### Streaming

`RequestDeadline` carries one monotonic budget across route attempts. When no explicit deadline is supplied, `RoutedBackend.streaming()` derives it from the caller timeout. Streaming promotes an available loopback route for a 1.2-second progress window before continuing through the remaining route chain. This affects timing only; `routes_for(tier)` retains the configured tier order.

The first route to emit meaningful text owns the answer. Its wrapped callback becomes the only output path, and later output from an abandoned worker is discarded. Once a route owns the answer, a failure ends that stream and no later route is contacted. This output gate prevents a slow local worker from duplicating a cloud response.

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

`RoutedBackend` groups routes by tier and tries each enabled, capable, unblocked route in configuration order. Deadline-aware streaming applies the local progress rule above. A route's timeout is the smaller of its own limit and the remaining caller budget. A provider failure, connection failure, timeout, model failure, auth failure, or empty response moves to the next candidate. An exhausted chain returns `None`.

Configured FAST and CHAT chains always end with loopback Ollama. The appended local FAST route has a 60-second route limit and the local CHAT route has a 180-second route limit, matching the local-only candidates; each caller can still impose a smaller timeout. Disabled configured entries remain visible in route status but cannot reduce those active local limits. A configuration with no routes has one effective local candidate per lane. `resolve_model()` returns a string-compatible value carrying its `Tier`, so existing backend method signatures remain ordinary model-string APIs while the router can select a chain.

A route the user switched off is inert. It stays in the chain so the control centre can still show it, but it takes no part in deciding how the local candidates are built: a configuration whose only route is disabled yields the same local chain as a configuration with no routes at all. The local candidates always run the configured `fast_model` and `ollama_chat_model`, and their timeouts leave room for a cold model load, because the local candidate is last in its chain and has nothing to fall forward to. A ceiling shorter than a page-in would not buy speed; it would guarantee the candidate can never answer.

### Model residency

Ollama unloads a model once its keep-alive lapses and resets that timer from each request's own `keep_alive`, applying its short default when the field is absent. Warming a model once is therefore not enough: an assistant that idles between conversations pays a cold page-in on the next thing the user says. Every route built against an Ollama runtime carries a `keep_alive`, and `OllamaBackend` stamps it onto each `direct`, `streaming`, and `chat` request unless the caller passed one explicitly. The duration is `30m`, or `1m` under `low_power_mode`, which trades warmth for handing the GPU back between turns. Remote OpenAI-compatible routes carry no residency: it is an Ollama knob and their servers own the decision.

`warm_up()` warms the first available candidate for the requested lane and its local Ollama candidate. `list_models()` combines unique names from reachable routes.

### Cooldown state

`RouteStateStore` writes `~/.jarvis/llm_routes_state.json` atomically with mode `0o600` where the platform supports POSIX permissions. The file contains route hashes and health counters, never URLs, models, or credentials.

| Failure | Block |
|---|---|
| 429 with `Retry-After` | Exactly the stated duration |
| 429 without a duration | 60 seconds, then 300 seconds, then 900 seconds |
| Quota exhaustion with reset | Until the stated reset |
| Quota exhaustion without reset | Until midnight UTC |
| 401 or 403 | Invalid for the process lifetime |

Hits, failures, last safe error label, and future block time feed the control centre. Persisted cooldowns prevent a restart from immediately touching a rate-limited or quota-exhausted key. Authentication invalidation is deliberately process-local, so a restarted process can retry a corrected external credential.

## Lanes

| Tier | Chain | Contexts |
|---|---|---|
| `Tier.FAST` | Configured fast routes, then local | intent judge, tool router, tool search, enrichment extractor, memory and tool digests, graph placement picker, evaluator, weather place extraction |
| `Tier.CHAT` | Configured chat routes, then local | reply loop, planner, step resolver, dictation cleanup, nutrition calls, other tool-specific completions |
| `Tier.PRIVATE` | loopback Ollama only | diary summary, deflection rewrite, topic optimisation, graph extraction, node merge, graph auto-split |

Memory retrieval may send the selected snippets into FAST or CHAT calls. Memory creation, graph mutation, and embeddings stay local.

## Embeddings

With `llm_routes` configured, `get_embedding_backend(cfg)` returns an `OllamaBackend` whose URL is loopback. It never returns `RoutedBackend`, and the model remains `cfg.ollama_embed_model`, preserving the vector space used by stored embeddings. A configured non-loopback Ollama URL falls back to `http://127.0.0.1:11434` for private and embedding work. A single-endpoint configuration with no routes retains its explicit embedding-provider behaviour.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `llm_routes` | `[]` | Ordered generic endpoint entries for FAST and CHAT |
| `llm_provider` | `"ollama"` | Single-endpoint protocol used when `llm_routes` is empty |
| `llm_base_url` | `""` | Single-endpoint URL used when `llm_routes` is empty |
| `llm_api_key` | `""` | Single-endpoint bearer credential used when `llm_routes` is empty |
| `llm_chat_model` | local model | Effective first CHAT model |
| `fast_model` | automatic | Effective first FAST model |
| `ollama_base_url` | `http://127.0.0.1:11434` | Ollama URL for a local-only setup; private work requires loopback |
| `ollama_chat_model` | setup selection | Local chat and private model |
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

The loader accepts this tiered shape and ignores malformed entries. List order is route order. A credential may be stored directly in `api_key` or referenced by `api_key_env`; environment values are resolved only when the backend is built and are never copied into configuration.

`provider` may also be `"claude_subscription"` (see "Claude subscription session" below). That route carries no `api_key`: `base_url` is still required for shape consistency but is never dialled, so any non-empty placeholder (conventionally `"claude-agent-sdk"`) satisfies it. `claude_subscription` is a `llm_routes`-chain provider only; it is not one of the choices for the single-endpoint `llm_provider` or `embedding_provider` settings, because those apply uniformly to the FAST tier and to embeddings, and a cloud subscription call has no place answering either: FAST needs a warm, low-latency local model, and embeddings must stay on loopback Ollama regardless of billing model. Every configured route entry that resolves to `Tier.PRIVATE` is dropped before a backend is built, for every provider without exception, so a `claude_subscription` route can never reach the private lane even if misconfigured with that tier.

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

`ClaudeSubscriptionBackend` authenticates through `claude_agent_sdk.ClaudeSDKClient`, which spawns Felix's own `claude` CLI subprocess and inherits whatever session it already has open. No `ANTHROPIC_API_KEY` is read, set, or stored for this route; there is no key to mask.

- Every call opens a fresh `ClaudeSDKClient`, sends one prompt, and disconnects. Jarvis's own `LLMBackend` contract already carries the full conversation on every call (`chat()` receives the whole `messages` list; `direct()` receives system and user text together), so resuming or continuing an SDK session across calls would duplicate that context rather than save anything. `chat()`'s multi-role `messages` list is flattened into one system prompt plus one labelled transcript string, because `ClaudeSDKClient.query()` takes a single prompt per call.
- The session is stripped to text generation only, because `ClaudeSDKClient` is otherwise a fully agentic session with its own tool-calling loop and Jarvis owns exactly one tool-calling loop and one security gate (`../security/security.spec.md`). Every session sets `tools=[]`, `setting_sources=[]`, and `mcp_servers={}`, and always passes a `can_use_tool` callback that denies every attempt unconditionally. The empty tool/settings/MCP options are not sufficient alone: an authenticated session can still see MCP tools attached at the Anthropic account level (connectors configured in the Claude.ai account the CLI is logged into), entirely outside this process's control, and the model can still attempt to call one. The `can_use_tool` deny-all callback is the mechanism that actually stops that attempt, and is mandatory rather than an alternative to the empty tool list. `permission_mode` is always `"default"`; a mode that auto-approves calls ahead of `can_use_tool` (e.g. `bypassPermissions`) would silently defeat the deny-all gate, per the SDK's own `CanUseToolShadowedWarning`.
- Every denied tool-use attempt and every backend selection is recorded through `debug_log`.
- No native tool schema is ever satisfiable (see "Tool calling" above): `chat()` raises `ToolsNotSupportedError` whenever `tools` is supplied, before any session is opened.
- No sampling controls: `num_ctx`, `thinking`, `temperature`, and `max_tokens` have no equivalent exposed by `ClaudeAgentOptions` for a single generation call, so they are accepted for signature parity and silently ignored, the same way `OpenAICompatibleBackend` ignores Ollama-only knobs it cannot express.
- No model-listing or warm-up endpoint: `list_models()` returns `[]` and `warm_up()` is a no-op returning `True`; nothing needs paging in and nothing is worth a real round trip at every daemon start.
- A failed `ResultMessage` (`is_error=True`) or a raised `ClaudeSDKError` maps to the same typed failures in the "Typed provider failures" table: `api_error_status` 401/403 to `AuthError`, 404 to `ModelUnavailableError`, 429 to `RateLimitedError`, anything else (including a missing `claude` CLI) to `ProviderError`. Every path is caught; nothing but these typed exceptions or an assembled string ever leaves the backend.
- `claude_agent_sdk` is an optional dependency, not listed in `requirements.txt`: it requires `mcp>=1.23.0,<3.0.0`, which conflicts with the `mcp==1.13.1` pin the persistent MCP runtime depends on (`../tools/external/mcp_runtime.spec.md`). The import is lazy and guarded, so the rest of Jarvis is unaffected without it installed, and a `claude_subscription` route used without the package installed fails with a typed `ProviderError` rather than an import crash.

## File layout

```text
src/jarvis/llm/
├── backend.py
├── claude_subscription.py
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
