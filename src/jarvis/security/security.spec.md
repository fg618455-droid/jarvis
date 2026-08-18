# Security Confirmation Gate

## Purpose

Every valid built-in and MCP tool passes through the security gate in
`run_tool_with_retries` before execution. The gate protects sensitive actions
without asking for approval for routine read-only work at the default level.
An unavailable confirmation path never permits execution.

## Levels

| Level | Behaviour |
|-------|-----------|
| `off` | Valid tools execute without confirmation. This level is intended for controlled development only. |
| `critical` | Every MCP tool, `deleteMeal`, `askCrew`, and `localFiles` write, append, or delete operations require confirmation. |
| `paranoid` | Every valid built-in and MCP tool requires confirmation. |

The default level is `critical`. An unknown level is treated as `critical`.
Unknown tools return the normal unknown-tool error and do not create a
confirmation request.

## Decision semantics

Configured channels are considered in order.

1. A missing or unavailable channel is skipped.
2. A channel that fails before producing a decision is skipped.
3. Approval permits the tool.
4. Refusal or timeout denies the tool and is final. The gate does not try a
   later channel because that would turn a refusal into a bypass.
5. No available channel denies the tool.

Gate errors also deny execution. The denied result is returned to the reply
engine as a failed tool result, and the tool implementation or MCP client is
not called.

The gate that decides is always the one matching the live settings. The bundled
desktop app runs the daemon inside its own process, so a daemon restart alone
would otherwise keep the previous level in force. A gate installed directly by
an embedding process carries no settings and is never replaced, so injected
channels survive.

Argument checks read values exactly as the tool reads them. `localFiles`
strips and lowercases its operation before acting, so the gate does the same
and surrounding whitespace cannot slip a mutation past confirmation.

## Synchronous execution boundary

`run_tool_with_retries` is synchronous and runs inside the reply engine's
synchronous agentic loop. The gate therefore exposes a synchronous channel
contract. A confirmation deliberately blocks that tool call until a decision
or timeout while audio callbacks, Qt's application thread, and subprocess log
threads remain available.

The channel implementations use their native blocking boundaries:

- Desktop confirmation sends a signal to the Qt application thread in bundled
  mode. Source mode sends a structured request over daemon stdout and receives
  the response over daemon stdin. A `threading.Event` carries the result back
  to the blocked tool thread.
- Web confirmation raises a card in the control centre and blocks on a
  `threading.Event` until a button is pressed or the timeout lapses. Server
  and daemon share one process, so no transport sits between them.
- Telegram sends its prompt over a blocking HTTPS request and then blocks on a
  `threading.Event`. It does not poll: one router owns the Bot API's update
  stream for the whole process and resolves the request when the decision
  arrives. It does not own or create an asyncio event loop.
- Voice confirmation speaks a challenge, consumes the active listener's audio
  queue, and transcribes the bounded response with the loaded Whisper backend
  under the same language and voice-activity settings as normal transcription.

No confirmation creates a fresh event loop. This avoids nested-loop failures
when Jarvis is embedded in a process that also runs asyncio and avoids the
startup and teardown cost of an event loop per protected tool.

## Desktop channel

The dialog displays the canonical tool name and JSON arguments. Approve and
deny are explicit buttons. Closing the dialog or reaching the configured
timeout denies the request. The channel requires no credentials.

## Web channel

The control centre shows the canonical tool name and its arguments with an
approve and a refuse button. The channel is available only while the control
centre is serving: a request nobody can see is worse than no channel at all,
because the gate would wait out its whole timeout before falling through.

It needs no credentials, which makes it the channel that answers on a setup
that runs the daemon alone and watches it in a browser. Such a setup has no Qt
tray for the desktop channel, and its speakers are not necessarily in the same
room as its user.

Every request is written to a decision log of the last fifty outcomes, kept in
memory and readable at `/api/security`. Approval, refusal, and timeout are all
recorded, so a tool that was refused while nobody was looking leaves a trace.

Reaching the control centre already means reaching the machine on loopback,
and requires the access token off it, so the channel's presence-evidence is
the server's own guard rather than a challenge of its own.

## Telegram channel

Telegram is available only when both `telegram_bot_token` and
`telegram_chat_id` are configured. Credentials may be supplied through the
settings file or the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment
variables. A configured value wins, and the environment is read only when the
setting is empty, so a token belonging to an unrelated project cannot hijack
the channel.

`telegram_api_base_url` names the Bot API server the channel calls. The Bot API
server is published as software, so pointing the key at a local instance keeps
tool names and arguments on the user's own machine and off a third party's.
The public host is the default because a bot token is useless without it.

The channel sends an inline-button request containing the tool and bounded
arguments. It accepts only an approve or deny callback whose random request ID
matches the pending request and whose chat ID matches the configured chat.
Requests, transport errors, refusal, and timeout all fail safely. Tokens are
not logged.

The decision travels through the shared Telegram router rather than a poll of
this channel's own, because the Bot API confirms updates by offset and a second
poller would delete what the first has not read. The request ID is claimed
before the prompt is sent, so an immediate tap still finds a pending request.
The same chat can also hold a conversation when `telegram_chat_enabled` is set.
See `../telegram/telegram.spec.md`.

## Voice and console channel

The voice channel generates a four-digit random challenge. Jarvis speaks the
tool name and spaced digits, then waits for one bounded microphone utterance.
Approval requires the transcription to contain exactly the same decimal digit
sequence. Unicode decimal digits are normalised, so the comparison does not
depend on English confirmation words or any other language-specific phrase.

An interactive console uses the same challenge when no voice requester is
registered. Non-interactive stdin is not an available confirmation channel.

Voice is the weakest channel because anyone in the room can hear and repeat
the code. Desktop or Telegram should appear earlier in the configured channel
order when stronger user presence or possession evidence is required.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `security_level` | `critical` | `off`, `critical`, or `paranoid` |
| `security_confirm_channels` | `desktop`, `web`, `telegram`, `voice` | Ordered channel names |
| `security_confirmation_timeout_sec` | `60` | Per-channel decision timeout, clamped to 1 through 300 seconds |
| `telegram_bot_token` | empty | Telegram Bot API credential |
| `telegram_chat_id` | empty | Sole chat authorised to decide requests |
| `telegram_api_base_url` | `https://api.telegram.org` | Bot API server to call; a self-hosted instance keeps confirmations local |
| `telegram_chat_enabled` | `false` | Whether the configured chat may also hold a conversation |

All keys are fields on the frozen `Settings` dataclass, are parsed and passed by
`load_settings()`, and are exposed on the Security page in the desktop settings
window and in the control centre's settings view.
