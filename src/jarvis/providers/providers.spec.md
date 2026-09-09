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
