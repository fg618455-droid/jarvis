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

Outbound requests carry `jsonrpc="2.0"`. The server omits that member from its
responses and notifications, so the reader accepts a message whose `jsonrpc`
member is absent and rejects one that carries any other value. The server also
emits unsolicited notifications such as `remoteControl/status/changed`, which the
reader queues alongside run events.

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

## Claude adapter

### Transport and authentication

`ClaudeAdapter` resolves `claude.cmd` before `claude` on Windows and `claude`
elsewhere, through `PATH`. A run is one `claude -p` process per session, started
with `--output-format stream-json --input-format stream-json
--include-partial-messages --verbose --session-id <uuid>`. Stdin stays open for
the lifetime of the session, which is what makes mid-run steering possible: a
further user message is a JSON line on stdin.

Only a claude.ai subscription starts a run. `AuthenticationManager` must report
`authMethod="claude.ai"`, and the child process starts through
`scrub_provider_environment()` so `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are
absent. `--bare` is never passed, because it switches the CLI to API-key
authentication.

### Models and capabilities

Claude Code has no model listing interface, so `list_models()` returns a
catalogue with `enumerable=False`. It starts empty and grows only from evidence:
the `model` field of a run's `init` event. A model the caller asked for and that
a run then confirmed is recorded as `verified-probe`; a model Claude chose by
itself is recorded as `provider-default`. No identifier is ever guessed.

`verify_model(name)` adds an entry without a full run. It sends a one-token
probe, `claude -p --output-format json --model <name> --permission-mode plan
"ping"`, and accepts the result only when `is_error` is false and `modelUsage`
names a model. The recorded identifier is the `canonicalModel` Claude billed,
not the alias the caller typed, so probing `sonnet` records `claude-sonnet-5`.
Any other outcome, an unknown model answered with `api_error_status` 404, output
that is not JSON, or a launch failure, records nothing and returns `None`. The
catalogue never grows from a failed probe.

`capabilities()` is closed until a run reports. The `init` event establishes
`tools` from its tool list and `mcp` from its MCP server list. `streaming`,
`steering` and `structured_output` follow from the transport the adapter itself
uses: the stream-json output format, the stream-json input format, and
`--json-schema`. Image input is not established by any observed evidence and
stays false.

### Capability profiles

| Capability profile | Claude permission mode |
|---|---|
| `read_only` | `plan` |
| `project_dev` | `acceptEdits` |
| `automation` | `dontAsk` |
| `unrestricted` | `bypassPermissions` |

`system_prompt` maps to `--append-system-prompt`, `mcp_config` to `--mcp-config`
with `--strict-mcp-config`, `additional_directories` to repeated `--add-dir`,
and `allowed_tools` to `--allowedTools`. `skills` and `toolsets` have no Claude
equivalent, so supplying one raises rather than being ignored.

### Event mapping

| Claude stream-json message | Normalised event |
|---|---|
| `system` / `init` | `run.started` |
| `stream_event` / `message_start` | `turn.started` |
| `content_block_delta` with `text_delta` | `text.delta` |
| `content_block_delta` with `thinking_delta` | `thinking` |
| `content_block_start` with a `tool_use` block | `tool.call` |
| `user` message carrying `tool_result` blocks | `tool.result` |
| `system` / `permission_request` | `approval.needed` |
| `result` | `usage.delta` then `run.finished` |

Tool arguments never reach an event: `tool.call` carries `args_redacted=True`
and `tool.result` carries a generic outcome and a byte count.

Terminal states stay apart. A successful `result` ends the run as `ok`, an
`api_error_status` of 429 ends it as `quota`, a stream that stops sending within
its message timeout ends it as `timeout`, and a stream that closes before a
`result` ends it as `error`.

### Sessions and usage

`list_sessions()` parses `claude agents --json` and marks a session as owned only
when this adapter started it. Sessions the user began in a terminal are listed
read-only. A listing that is not JSON, or not a list, raises rather than
degrading to an empty result.

`resume()` and `fork()` return `NotSupported`. The CLI can continue a session
through `--resume`, but only together with a new user message, which the
session-only signature of the contract cannot carry. Reporting that honestly is
preferable to a resume that silently invents a prompt.

Claude has no usage query command. A run emits `rate_limit_event` carrying
`status`, `rateLimitType`, `utilization` and `resetsAt`, and its `result` event
carries token counts. The adapter keeps the last reading, so `usage()` is
unavailable until a run has happened in this process and reports the real
window afterwards. A `status` that does not begin with `allowed` is treated as
an exhausted window and terminates the run as `quota`; `allowed_warning` is a
warning and does not.

## Hermes adapter

### Transport and authentication

`HermesAdapter` resolves the launcher through the same
`%LOCALAPPDATA%/hermes/bin/hermes.cmd` discovery as `AuthenticationManager`,
with `hermes` as the PATH fallback. Its only transport is one long-lived
`hermes acp` process using Agent Client Protocol JSON-RPC v2 over stdio. ACP
startup may take about 30 seconds, so initialisation has a 60-second request
budget. `HermesTransportState` is either `acp` or `unavailable` and exposes a
failed startup rather than selecting another execution mode.

There is no `hermes -z` fallback. That command emits only final text and
auto-bypasses approvals, so it cannot preserve either the event or permission
contract. `--yolo` is never passed. Every Hermes child receives
`scrub_provider_environment()`, so neither billing API key is present.

The authentication manager reads the configured provider at runtime and runs
`hermes auth status <provider>`. A run starts only when that exact status is a
subscription route represented as `method="chatgpt"`; no provider name is
hard-coded by the adapter.

### Models and capabilities

`list_models()` reads `models.availableModels` from the ACP `session/new`
reply, taking each `modelId` with `source="api"` and its `name` as the display
name. That is the provider's own structured advertisement, so nothing is
refreshed through a command, nothing is read from a cache file that could be
stale, and no identifier is inferred. Opening a session is the only way to ask,
so `list_models()` starts one when the adapter has not yet seen a catalogue.
Missing, malformed or changed required fields raise `HermesProtocolError`.

`session/new` also returns `models.availableModels` and `currentModelId`. A
caller-selected model is accepted only when it appears in that response, then
is pinned with `session/set_model` and recorded with `model_source="api"`. With
no selection, the reported `currentModelId` is recorded with
`model_source="provider-default"`. An absent current model remains unknown.

Capabilities start false. A successful `initialize` establishes image input
only from `agentCapabilities.promptCapabilities.image`. Streaming is true
because the selected ACP transport demonstrably sends `session/update`
notifications. Tools, MCP, structured output and steering have no positive
flag in the handshake and remain false.

### Capability profiles and approvals

The modes returned by `session/new` are the only approval policies the adapter
uses:

| Capability profile | Required ACP mode | Behaviour |
|---|---|---|
| `read_only` | `default` | Hermes asks before edits; the JARVIS ACP client declines each approval request. |
| `project_dev` | `accept_edits` | Hermes auto-allows workspace and temporary-directory edits and asks for sensitive paths. |
| `automation` | `dont_ask` | Hermes auto-allows session file edits except sensitive paths. Any remaining approval request is declined. |
| `unrestricted` | none | Not faithfully representable without `--yolo`; run creation raises `HermesProtocolError`. |

Run creation fails if the required mode is absent from the session response.
An ACP `session/request_permission` becomes `approval.needed`, with option IDs
but without tool arguments, and receives a cancelled outcome. This is
fail-closed because `ProviderAdapter` has no operation for returning an approval
decision in this phase.

`system_prompt`, `mcp_config`, `allowed_tools`, `additional_directories`,
`skills` and `toolsets` have no wired ACP mapping in this phase. Supplying any
of them raises rather than being ignored.

### Event mapping

| Hermes ACP message | Normalised event |
|---|---|
| successful session and prompt dispatch | `run.started`, then `turn.started` |
| `agent_message_chunk` | `text.delta` |
| `agent_thought_chunk` | `thinking` |
| `tool_call` | `tool.call` |
| terminal `tool_call_update` | `tool.result` |
| `session/request_permission` | `approval.needed` |
| `session/prompt` reply | exact token `usage.delta`, then `run.finished` |

`available_commands_update` and `session_info_update` carry no provider-neutral
event and are ignored. Tool input is never copied into an event;
`tool.call.args_redacted` is always true. Tool results contain only success,
generic summary and byte count.

`session/prompt.stopReason="end_turn"` maps to `ok`, and `cancelled` maps to
`cancelled`. Every other stop reason maps to `error`. A message timeout maps to
`timeout`, while a closed or malformed stream maps to `error`.

Hermes exposes no structured quota outcome. The adapter never emits
`run.finished(status="quota")` and never searches assistant text for quota-like
phrases. Provider failures that arrive as assistant prose remain prose; an
unknown stop reason is `error`.

### Sessions, usage and health

The handshake currently advertises session `resume`, `fork` and `list`; the
adapter maps them to `session/resume`, `session/fork` and `session/list`.
Interruption uses the ACP `session/cancel` method. Mid-run steering has no
advertised capability and returns `NotSupported`. Any absent session capability
also returns `NotSupported` rather than simulating success.

`usage()` is unavailable before a run. It then reports the exact input, output
and cached-read token counts from the `session/prompt` reply, and mentions the
latest `usage_update` context reading in its detail.

`usage_update` describes how full the session context is, not what a turn spent,
so it produces no `usage.delta` event and never populates `limit` or
`remaining`. Those fields stay empty because Hermes has no quota window of its
own: it spends the subscription of the provider configured in its own settings,
and shares that budget with whichever adapter uses the same provider. On a
machine configured with `openai-codex`, Hermes and the Codex adapter draw on one
ChatGPT subscription, so a failover between them buys nothing.

`health()` starts and initialises ACP, reports the advertised agent name and
version when present, and measures the complete reachability check latency. A
startup or protocol failure reports `reachable=False` with the transport state;
it never falls back to `-z`.

## Cross-adapter contract tests

`tests/providers/test_golden_contract.py` holds the rules every adapter obeys,
so a fourth one cannot quietly invent its own shape. Each adapter contributes a
recording of a real session under `tests/providers/golden/`, and the suite
asserts against all of them together:

- Every emitted event is a kind the contract knows, carrying its required payload.
- The assistant's answer reaches the caller as `text.delta` and matches the text
  in the recording.
- A terminal status is one of the five; no adapter invents a sixth.
- No normalised payload carries provider content, credentials or key material.
- Usage counts are non-negative integers.
- A tool call announces its name and identifier and redacts its arguments. This
  is checked by feeding each adapter a tool call whose arguments contain
  credentials and reading what escapes, not only by inspecting recordings that
  happen to contain no tools.

Adding an adapter without a recording fails the suite.

## Registry and diagnostic command

`registry.get_provider(id)` builds one adapter and `registry.list_providers()`
names all of them. An unknown identifier raises with the known names in the
message rather than returning a null adapter.

`python -m jarvis.providers.cli status [provider ...]` prints what each provider
currently reports: authentication, model catalogue, usage, health with latency,
and visible sessions. It is a diagnostic, so it is built to stay honest under
failure:

- Each reading is taken independently. One provider that cannot answer, or one
  CLI that is not installed, is reported in place and does not stop the rest.
- Absent evidence is printed as absent. An empty catalogue says whether the
  provider cannot list models at all or simply has not confirmed one yet, and an
  unavailable usage reading prints the provider's own reason.
- Nothing is estimated or filled in with a plausible zero.

The command reconfigures stdout to UTF-8 because a Windows console defaults to
cp1252, which cannot encode the emojis in the report.
