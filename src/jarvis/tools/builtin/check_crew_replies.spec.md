## checkCrewReplies Spec

### Purpose

Let Jarvis check what a specialist agent in the self-hosted Hermes crew has posted in its own Telegram topic since Jarvis started watching — the explicit, on-demand counterpart to `askCrew` (`ask_crew.spec.md`), which delegates but never reads back.

### Principle

`askCrew` stays fire-and-forget on purpose: a task that needs the crew's depth can take minutes, and blocking the calling conversation on Hermes' pace would defeat the point of delegating in the first place. `checkCrewReplies` does not change that — it never waits either. It answers exactly one question: "has anything shown up yet?", using whatever the router already captured.

### Design

The Telegram Bot API has no endpoint to fetch message history; a bot only ever learns about a message via `getUpdates` while it is polling. So this tool cannot retrieve a reply that arrived before Jarvis was listening — it reads a bounded, in-memory buffer that `TelegramRouter` keeps per watched `(chat_id, thread_id)` (see `../../telegram/telegram.spec.md`, "Crew topic watch"). The buffer survives only as long as the process does; a restarted daemon starts watching again but has no memory of what came before.

To make a reply visible even when nobody happens to call this tool right after Hermes answers, the daemon watches every crew topic as soon as `crew_telegram_chat_id` is configured — independent of whether the personal Telegram chat channel is on. `checkCrewReplies` also calls `watch_topic` and `ensure_polling` itself, defensively, so a standalone invocation still starts capturing rather than silently watching nothing.

### Contract

- **Name**: `checkCrewReplies`
- **Input schema**:
  - `agent` (string, required, enum): one of `jarvis`, `dev`, `research`, `assistant`, `schule`, `scribe`, `reach` — the same roster `askCrew` uses (`AGENT_THREADS` in `ask_crew.py`).
- **Output**: on success, either "no replies yet" or the buffered messages as raw `- {sender}: {text}` lines (raw data, per this codebase's tools-return-raw-data convention — the reply engine formats it for the user). On failure, a stable `ToolExecutionResult` error (`invalid_argument` for an unknown agent, `invalid_config` when the crew channel or Telegram itself isn't set up).

### Security

Not in `security/gate.py`'s `_CRITICAL_BUILTINS` — this is a read-only look at what Jarvis' own bot already received, with no outbound action and no new exposure beyond what `askCrew`'s own confirmation already governs.

### What checkCrewReplies is NOT

- Not a history lookup: it cannot see anything that arrived before the router was watching that topic.
- Not a wait: a call with nothing buffered yet returns immediately, saying so.

### Testing

Behaviour tests (`tests/tools/builtin/test_check_crew_replies.py`) mock the router and cover: unknown agent, empty `crew_telegram_chat_id`, an unavailable router (both without a network call), no buffered messages, buffered messages formatted as raw lines, the `jarvis` agent reading the General topic (`thread_id=None`), and that the tool watches the agent's own topic before reading. Router-level buffering itself is covered in `tests/test_telegram_router.py`. A gate test (`tests/test_security_gate.py`) confirms `checkCrewReplies` does not require confirmation.
