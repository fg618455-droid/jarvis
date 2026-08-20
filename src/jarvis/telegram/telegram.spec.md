# Telegram Specification

Telegram is a way in and out of the assistant: a chat that raises security
confirmations, and, when switched on, a conversation channel that reaches the
same assistant as the microphone and the chat window.

## One poller, and why

The Bot API delivers updates through `getUpdates`, and it treats an update as
confirmed as soon as `getUpdates` is called with an offset above that update's
id. `allowed_updates` does not filter updates that already existed when the
call was made.

Two pollers on one bot token therefore destroy each other's work: whichever
advances the offset first deletes what the other has not read. A message typed
while a confirmation was pending would never arrive, and no error would say so.

`TelegramRouter` is the only thing in the process that calls `getUpdates`.
Everything else registers with it. `get_router_for()` is the only way to obtain
one, and it returns the same router for the same credentials, so no call site
can create a second poller by accident. A changed token, chat, or API host is a
different bot: the previous router is stopped and replaced.

## The polling thread never waits for a reply

A turn started from Telegram can raise a Telegram confirmation. If the polling
thread were the thread running that turn, it could not fetch the decision that
would release it, and the turn would block until the confirmation timed out.

So the router dispatches and returns. Message handling hands the work to the
daemon's fire-and-forget submission, which owns its own worker thread, and the
reply is sent from that thread. What the router does inline is bounded: reading
an update, and the short Bot API calls that send a message or a typing hint.

## Router

| Behaviour | Rule |
|-----------|------|
| Availability | A bot token and a chat id, both non-empty |
| Backlog | Discarded on start: the offset jumps past the newest queued update |
| Offset | Advanced past every update, including ignored ones |
| Authorisation | Only the configured chat id reaches the confirmation/chat handler; anything else is dropped there |
| Errors | A failed poll is logged and retried; the loop does not die |
| Start | Idempotent and serialised; a running router is never doubled |
| Stop | Bounded join. A thread still inside a long poll keeps its slot until it returns, so nothing starts a second poller beside it |

Discarding the backlog is what keeps an instruction sent last night from
running at breakfast because the daemon happened to come back up.

The router polls whenever it is needed: the daemon starts it when the chat
channel is on or a crew chat id is configured, and a confirmation starts it on
demand in a process where nothing else has, such as a standalone control
centre.

## Confirmations

`TelegramConfirm` owns the prompt and its buttons; the router owns the answer.
The channel claims a random request id, sends an inline keyboard, and blocks on
an event until the router resolves it or the timeout lapses.

The request id is claimed *before* the prompt is sent, so a decision taken the
instant the message lands still finds something to resolve.

A decision counts only when the callback carries the matching request id and
comes from the configured chat. Refusal and timeout are both final, and neither
is ever reported as approval. See `../security/security.spec.md` for the policy
that decides which tools ask in the first place.

## Conversation

Off unless `telegram_chat_enabled` is set. Approving an action someone else
started is a smaller grant than starting one: a message here runs tools on this
machine, so it is opt-in rather than a side effect of configuring a bot token.

A message from the configured chat becomes a turn through the daemon's shared
text entry point, which means one conversation across voice, the chat window,
the control centre, and Telegram: the same dialogue memory, the same tools, the
same security gate, and one diary entry at the end of the session. Redaction
happens inside the reply engine, as it does for every other entry point.

| Situation | What the user gets |
|-----------|--------------------|
| A turn is accepted | A typing indicator, then the reply |
| A turn is already running | A note that the assistant is busy; the message is refused, not queued |
| The turn produced nothing | An honest failure, never a fabricated answer |
| A message that is not text | A note that only text is read; no turn runs |
| Longer than 4000 characters | Refused with the limit named; no turn runs |
| A reply over 4096 characters | Split into whole messages, losing nothing |

## Crew topic watch

The Bot API has no endpoint to fetch message history — a bot only ever sees a
message via `getUpdates` while it is polling. `watch_topic(chat_id,
thread_id)` opts one more `(chat_id, thread_id)` pair into an in-memory,
per-scope buffer (bounded to the most recent messages); `get_topic_messages`
reads a snapshot back. This is additive and independent of `self.chat_id`: it
never changes what the confirmation and chat channel treat as authorised, and
watching a scope that is never granted a matching message simply buffers
nothing. See `../tools/builtin/check_crew_replies.spec.md` for the tool built
on top of it.

The buffer is process memory only — it does not survive a restart, and
`thread_id=None` matches a forum's General topic the same way `AGENT_THREADS`
in `ask_crew.py` already represents it.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `telegram_bot_token` | empty | Bot API credential |
| `telegram_chat_id` | empty | The sole chat that may decide or talk |
| `telegram_api_base_url` | `https://api.telegram.org` | Bot API server to call |
| `telegram_chat_enabled` | `false` | Whether the configured chat may hold a conversation |

The token and chat id fall back to `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`,
and a configured value wins, so a token belonging to an unrelated project
cannot hijack the channel.

The Bot API server is published as software. Pointing `telegram_api_base_url`
at a local instance keeps tool names, arguments, and the conversation itself on
the user's own machine. The public host is the default only because a bot token
is useless without one. Nothing in this package addresses the API host
directly; it is always read from the setting.

Tokens are never logged.
