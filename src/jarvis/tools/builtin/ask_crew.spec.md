## askCrew Spec

### Purpose

Let Jarvis delegate a task to a specialist agent in the self-hosted Hermes crew (a separate, always-on system running on the NAS) when a task needs more time, tool depth, or reasoning power than a quick local reply.

### Principle

The reasoning backend defaults to local for simple, fast turns — that keeps every ordinary exchange private and free. `askCrew` is the deliberate, explicit exception: a named tool call, gated by confirmation, used only when a task is genuinely heavier than the local model should carry. It is not a silent fallback and not the default path.

### Design

Jarvis has no direct API into Hermes and does not run it. The one channel Hermes already watches and answers through is its own Telegram group ("Mission Control"), with one topic per agent. `askCrew` posts the task into the target agent's topic and returns immediately — it never waits for or reads back Hermes' reply. The result surfaces to the user later, in that Telegram channel or the shared vault, on Hermes' own schedule, not inline in the calling conversation.

This keeps the tool a thin, stateless HTTP call (`sendMessage` via `RequestsTelegramTransport`), with no polling loop, no cross-bot reply correlation, and no new privileged surface on the NAS.

### Contract

- **Name**: `askCrew`
- **Input schema**:
  - `agent` (string, required, enum): one of `jarvis`, `dev`, `research`, `assistant`, `schule`, `scribe`, `reach` — the fixed roster of the crew.
  - `task` (string, required): the task to delegate, in the user's own words.
- **Output**: on success, a short confirmation that the task was delegated. On failure, a stable `ToolExecutionResult` error (`invalid_argument` for an unknown agent or empty task, `invalid_config` when the crew channel isn't set up, `unavailable` — retryable — when Telegram can't be reached).

### Configuration

- `crew_telegram_chat_id`: the Mission Control group's chat ID. Empty disables the tool (`invalid_config`).
- Reuses the bot already configured under `telegram_bot_token` / `telegram_api_base_url` (Security → Telegram) — that bot must also be a member of the Mission Control group, and Hermes' own `TELEGRAM_ALLOWED_USERS` allowlist must include it, or Hermes will silently ignore the message. Both are one-time setup steps outside this codebase.

### Topic mapping

The agent → Telegram topic mapping (`AGENT_THREADS` in `ask_crew.py`) mirrors the layout `docs-felix/nas-scripts/topics-erfassen.sh` establishes on the Hermes side. The two are coupled by construction: changing the group's topics means updating both.

### Security

`askCrew` is always critical (`security/gate.py`'s `_CRITICAL_BUILTINS`), regardless of the configured security level's other exemptions — every delegation to the crew asks for confirmation first, the same way an MCP tool call would. This is the point at which the security gate closes the gap Hermes itself does not have: whatever the user delegates through `askCrew` is confirmed; whatever they type directly into Telegram is not.

### What askCrew is NOT

- Not a synchronous request/response bridge: the caller does not get the crew's answer back in this tool call or this conversation turn.
- Not a way to reach Hermes outside the crew's own Telegram group — no direct NAS API call, no new NAS-side privileged endpoint.
- Not the default reasoning path — see the offline-first note in the project's `CLAUDE.md` for the boundary between "local by default" and "delegated on purpose".

### Testing

Behaviour tests (`tests/tools/builtin/test_ask_crew.py`) mock `RequestsTelegramTransport` and cover: unknown agent, empty task, missing configuration (both without any network call), a successful send's exact payload shape (including the no-thread-id case for `jarvis`), and a network failure reported as retryable. A gate test (`tests/test_security_gate.py`) confirms `askCrew` always requires confirmation.
