## askCrew Spec

### Purpose

Let Jarvis delegate a task to a specialist agent in the self-hosted Hermes crew (a separate, always-on system running on the NAS) when a task needs more time, tool depth, or reasoning power than a quick local reply.

### Principle

The reasoning backend defaults to local for simple, fast turns. `askCrew` is the deliberate exception, and every invocation remains gated by confirmation. It has two triggers: the model may choose the named tool for a task that is clearly heavy, and the reply engine may invoke the same tool path when the measured local turn exceeds its deadline. The deadline trigger is not a general cloud fallback. It is the specific Hermes carve-out described in `CLAUDE.md`.

### Design

Jarvis has no direct API into Hermes and does not run it. The one channel Hermes already watches and answers through is its own Telegram group ("Mission Control"), with one topic per agent. `askCrew` posts the task into the target agent's topic and returns immediately — it never waits for or reads back Hermes' reply. The result surfaces to the user later, in that Telegram channel or the shared vault, on Hermes' own schedule, not inline in the calling conversation.

This keeps the tool a thin, stateless HTTP call (`sendMessage` via `RequestsTelegramTransport`), with no polling loop, no cross-bot reply correlation, and no new privileged surface on the NAS.

The automatic path lives in `reply/engine.py`. It bypasses the model's tool-choice decision, not the tool registry or security gate, and calls `run_tool_with_retries` with `agent="jarvis"` and the redacted user request. The explicit and automatic paths therefore share validation, confirmation, Telegram transport, and tool telemetry.

Their acknowledgements are written for different readers, so they are not the same words. The tool's `reply_text` is read by a model that will rewrite it, and can therefore carry instructions about what not to claim. `spoken_acknowledgement()` is what the automatic deadline delivers straight to the speakers with no model in between, so it is finished prose addressed to the person listening, rendered through `in_the_voices_language`.

### Contract

- **Name**: `askCrew`
- **Input schema**:
  - `agent` (string, required, enum): one of `jarvis`, `dev`, `research`, `assistant`, `schule`, `scribe`, `reach` — the fixed roster of the crew.
  - `task` (string, required): the task to delegate, in the user's own words.
- **Output**: on success, a short confirmation that the task was delegated. On failure, a stable `ToolExecutionResult` error (`invalid_argument` for an unknown agent or empty task, `invalid_config` when the crew channel isn't set up, `unavailable` — retryable — when Telegram can't be reached).
- **Explicit trigger**: the local model emits an `askCrew` tool call.
- **Automatic trigger**: `crew_handoff_enabled` is on, both `telegram_bot_token` and `crew_telegram_chat_id` are configured, a `TurnTrace` is active, and the reply engine reaches either deadline:
  - at 3 seconds, hand off unless the router made a positive no-tool decision, all local tool steps have results and only final synthesis remains, or a complete natural-language response has arrived;
  - at 5 seconds, hand off regardless of the close-to-done signal.
- **Automatic task**: the redacted user request is delegated to the general `jarvis` crew topic.

### Configuration

- `crew_handoff_enabled`: off by default. The automatic deadline shares askCrew's confirmation requirement (see Security below), and that wait is not bounded to the deadline, so turning this on before a fast or unattended confirmation path exists means an escalation can sit at the full `security_confirmation_timeout_sec` before falling through to a refusal instead of an answer — worse than letting the local reply keep running. With the flag off, the local route runs to its ordinary caller and route timeouts; disabled external route entries do not impose a four-second local cap. The explicit, model-chosen trigger is unaffected by this flag.
- `crew_telegram_chat_id`: the Mission Control group's chat ID. Empty disables the tool (`invalid_config`).
- Reuses the bot already configured under `telegram_bot_token` / `telegram_api_base_url` (Security → Telegram) — that bot must also be a member of the Mission Control group, and Hermes' own `TELEGRAM_ALLOWED_USERS` allowlist must include it, or Hermes will silently ignore the message. Both are one-time setup steps outside this codebase.

### Topic mapping

The agent → Telegram topic mapping (`AGENT_THREADS` in `ask_crew.py`) mirrors the layout `docs-felix/nas-scripts/topics-erfassen.sh` establishes on the Hermes side. The two are coupled by construction: changing the group's topics means updating both.

### Security

`askCrew` is always critical (`security/gate.py`'s `_CRITICAL_BUILTINS`), regardless of the configured security level's other exemptions — every delegation to the crew asks for confirmation first, the same way an MCP tool call would. This is the point at which the security gate closes the gap Hermes itself does not have: whatever the user delegates through `askCrew` is confirmed; whatever they type directly into Telegram is not.

The automatic deadline does not bypass confirmation. A refusal or unavailable confirmation channel ends the local turn with an honest failure message saying that no crew answer will follow.

### Handoff ownership and user experience

The deadline decision owns the turn. Local content arriving after a handoff decision is discarded, and the local loop cannot deliver a second answer. Jarvis returns only the delegation acknowledgement.

That acknowledgement has to be unmistakable about the one thing there is no channel for. There is no return path from the crew into this conversation, so a user who hears "I'll let you know once it's done" waits for a message that will never arrive, which is a worse outcome than being told plainly to look elsewhere. Stating where the result appears is not enough on its own: a model given only that fact still writes the follow-up promise, because that is what a helpful assistant says. Both acknowledgements therefore say explicitly that nothing about this task comes back here, and the tool's model-facing text forbids the promise in as many words.

### What askCrew is NOT

- Not a synchronous request/response bridge: the caller does not get the crew's answer back in this tool call or this conversation turn.
- Not a way to reach Hermes outside the crew's own Telegram group — no direct NAS API call, no new NAS-side privileged endpoint.
- Not the default reasoning path. Simple replies remain local, while the measured 3-second and 5-second deadline exception prevents a slow local turn from continuing indefinitely. See the reasoning-backend boundary in `CLAUDE.md`.

### Testing

Behaviour tests (`tests/tools/builtin/test_ask_crew.py`) mock `RequestsTelegramTransport` and cover: unknown agent, empty task, missing configuration (both without any network call), a successful send's exact payload shape (including the no-thread-id case for `jarvis`), a network failure reported as retryable, and that the acknowledgement states no answer returns to this conversation and forbids promising a follow-up. A gate test (`tests/test_security_gate.py`) confirms `askCrew` always requires confirmation. Engine tests (`tests/test_reply_crew_handoff.py`) cover both deadlines, the structural close-to-done predicate, shared pre-flight budgets, single-answer ownership, that the spoken acknowledgement carries no model-facing instruction, the `crew_handoff` stage, that `crew_handoff_enabled` defaults off with the deadline otherwise fully configured, and that `load_settings()` wires the flag from a real config file.
