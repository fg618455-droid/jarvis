# Subscription provider contract

## Scope

`jarvis.providers` is the provider-neutral boundary for subscription-backed agent
runs. It is headless, has no Qt dependency and is not connected to the reply or
voice pipelines.

The contract is run-oriented. A run pins its provider, model, session and working
directory in a `RunHandle`. Provider adapters do not expose a completion-style
fallback API.

## ProviderAdapter

Every adapter implements this complete interface:

| Method | Result | Contract |
|---|---|---|
| `id()` | `str` | Stable identifier: `claude`, `codex` or `hermes`. |
| `auth_status()` | `AuthStatus` | Subscription authentication state. Unknown or malformed state fails closed. |
| `list_models()` | `ModelCatalog` | Models obtained from provider-backed evidence. `enumerable` states whether the provider supplies a listing interface. |
| `capabilities(model)` | `Capabilities` | Known tools, MCP, streaming, image, structured-output and steering support. Unknown flags are false. |
| `start_run(spec)` | `RunHandle` | Starts a run and pins its execution choices. |
| `stream(run)` | `Iterator[RunEvent]` | Emits only normalised provider events. |
| `steer(run, text)` | `bool \| NotSupported` | Sends another user message during a run. |
| `interrupt(run)` | `bool \| NotSupported` | Stops an active run. |
| `resume(session_id)` | `RunHandle \| NotSupported` | Resumes a session under JARVIS control. |
| `fork(session_id)` | `RunHandle \| NotSupported` | Creates a JARVIS-owned branch of a session. |
| `list_sessions()` | `list[SessionInfo] \| NotSupported` | Includes external sessions, which are read-only until explicitly forked. |
| `usage()` | `UsageSnapshot` | Reports provider usage or `available=False`; values are never estimated. |
| `health()` | `HealthReport` | Reports reachability and measured latency. |

Unsupported steering, interruption, resume, fork and session-list operations return
`NotSupported`. Adapters do not simulate missing features or switch provider
implicitly.

`RunSpec.capability_profile` is one of `read_only`, `project_dev`, `automation` or
`unrestricted`, and defaults to `read_only`. Each adapter translates that neutral
profile into its native permissions. `RunSpec.mcp_config` carries an optional path
to an MCP configuration without prescribing a provider-specific flag.

## Normalised events

`RunEvent` accepts exactly these event kinds and requires the listed payload fields:

| Kind | Required payload |
|---|---|
| `run.started` | `run_id`, `provider`, `model`, `session_id`, `cwd`, `ts` |
| `turn.started` | `turn_id` |
| `text.delta` | `text` |
| `thinking` | `text` |
| `tool.call` | `name`, `args_redacted`, `tool_id` |
| `tool.result` | `tool_id`, `ok`, `summary`, `bytes` |
| `approval.needed` | `kind`, `detail`, `options` |
| `needs_you` | `question` |
| `usage.delta` | `input`, `output`, `cached`; optional `cost_hint` |
| `run.finished` | `status`, `reason` |

Terminal statuses are `ok`, `error`, `cancelled`, `quota` and `timeout`. `quota`
is a first-class terminal state and never triggers an implicit provider switch.

## Evidence and fail-closed defaults

Model sources are `api`, `verified-probe`, `provider-default` and `unknown`. A
model identifier must not enter the catalogue from assumption or training
knowledge.

Every provider returns a `ModelCatalog`. Providers without a model-listing interface
return `enumerable=False`; their catalogue can still contain models established by
verified probes or successful run events.

`AuthStatus`, `Capabilities`, `UsageSnapshot` and `HealthReport` default to their
unavailable or false states. Callers can therefore distinguish absent evidence
from a positive result.

Provider values are immutable data objects. Timestamps use serialisable ISO 8601
strings, and paths remain strings so adapters can preserve native platform paths.

## Authentication

`AuthenticationManager.status(provider)` reads authentication only through the
provider's status command and caches each result for 60 seconds. Failed commands,
non-zero exits, malformed output and unrecognised authentication modes all produce
`AuthStatus(logged_in=False)`. Cache entries can be invalidated explicitly after a
login or logout.

Claude accepts only the JSON schema from `claude auth status` with
`authMethod="claude.ai"`. Codex accepts only `Logged in using ChatGPT`. Hermes
reads `model.provider` from `%LOCALAPPDATA%/hermes/config.yaml`, passes that value to
`hermes auth status <provider>`, and accepts only the exact successful status line.
A missing, unreadable or empty provider setting fails closed with
`detail="hermes provider unknown"`. API-key and other Hermes routes are reported as
unauthenticated.

`hermes status` is not executed or parsed because it is network-dependent and can
display masked credential fragments.

Every provider subprocess receives a copied environment with
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` removed. Provider output, account details
and environment values are not written to debug logs.

## Capability evidence

`ProviderCapabilityRegistry` starts empty. Unknown provider and model capabilities
therefore resolve to a `Capabilities` value whose flags are all false.

Adapters register complete provider-level evidence when a feature belongs to the
transport, and complete model-level evidence when the provider reports capabilities
for a specific model. Model-level evidence replaces the provider-level value rather
than merging with it. This prevents an unsupported model feature from becoming true
through an optimistic fallback.

Capability evidence is isolated by provider and can be cleared after a provider or
model state change. The registry does not contain a guessed built-in capability
matrix.

## Codex adapter

### Transport and authentication

`CodexAdapter` resolves `codex.cmd` on Windows and `codex` on other platforms
through `PATH`. Its primary transport is one long-lived `codex app-server`
process using JSON-RPC v2 over stdio. Stdin remains open for the lifetime of the
server. Requests, responses, notifications and server requests use the shapes
generated by `codex app-server generate-json-schema --out <dir>` for Codex CLI
0.153.4.

The adapter checks `AuthenticationManager` before starting provider work and
accepts only `Logged in using ChatGPT`. After app-server initialisation it calls
`account/read` and requires `account.type="chatgpt"`. Both checks fail closed.
Every provider child receives `scrub_provider_environment()`, so
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are absent.

If app-server cannot start or complete initialisation, run creation can use
`codex exec --json`. `CodexTransportState` exposes `mode="exec-jsonl"`, and
`health()` includes the fallback in its detail. Failures after a working
app-server connection do not switch transports. The fallback cannot enumerate
models or sessions, provide account usage, resume or fork without a new prompt,
or steer an active turn. Those operations fail explicitly or return
`NotSupported`; they do not report simulated success.

### Models, capabilities and runs

`list_models()` follows `model/list` pagination and creates only `ModelInfo`
entries returned by the provider, with `source="api"`. A missing or changed
required response field raises `CodexProtocolError`; no model identifier is
inferred. A caller-selected model starts only when the current catalogue
confirms it. Runs without a selected model record the model returned by
`thread/start` and use `model_source="provider-default"`.

`capabilities(model)` reads `modelProvider/capabilities/read`. `namespaceTools`
or `webSearch` establishes `tools=True`, and `imageGeneration` establishes
`images=True`. The response has no flags for MCP, streaming, structured output
or steering, so those values remain false. A malformed capability response
returns the all-false value.

Run creation calls `thread/start` followed by `turn/start`. The selected model is
sent to both requests. The neutral capability profiles map as follows:

| Capability profile | Codex sandbox | Approval policy |
|---|---|---|
| `read_only` | `read-only` | `on-request` |
| `project_dev` | `workspace-write` | `on-request` |
| `automation` | `workspace-write` | `never` |
| `unrestricted` | `danger-full-access` | `on-request` |

`system_prompt` maps to `developerInstructions`. `mcp_config`, `allowed_tools`,
`additional_directories`, `skills` and `toolsets` are not wired to app-server in
this phase. Supplying one of them raises `CodexProtocolError` rather than being
ignored.

### Event mapping

| Codex app-server message | Normalised event |
|---|---|
| successful `thread/start` and `turn/start` | `run.started` |
| `turn/started` | `turn.started` |
| `item/agentMessage/delta` | `text.delta` |
| `item/reasoning/summaryTextDelta`, `item/reasoning/textDelta` | `thinking` |
| tool-like `item/started` | `tool.call` |
| tool-like `item/completed` | `tool.result` |
| command, file and permission approval server requests | `approval.needed` |
| user-input and MCP elicitation server requests | `needs_you` |
| `thread/tokenUsage/updated` | `usage.delta` |
| `turn/completed` or a non-retrying `error` | `run.finished` |

Tool arguments are never copied into normalised events. `tool.call` contains
`args_redacted=True`; `tool.result` contains a generic outcome summary and a byte
count only. `usageLimitExceeded`, `rateLimitExceeded` and
`sessionBudgetExceeded` terminate with `status="quota"`. Completed, interrupted
and other failed turns terminate as `ok`, `cancelled` and `error` respectively.
A stream that stops producing messages within its message timeout terminates with
`status="timeout"`, which is distinct from a transport failure reported as
`status="error"`.

`usage()` reads both `account/usage/read` and `account/rateLimits/read`. The
primary window is reported as a percentage limit with its provider reset time.
Token totals are not reclassified as input or output token usage. Missing or
changed readings return `UsageSnapshot(available=False)` with an explicit
detail. `health()` measures an `account/read` round trip and reports its latency.
