# Morning School Briefing Specification

The morning school briefing is an optional, short spoken summary of the fixed
School graph branch. It covers upcoming examinations, homework deadlines, and
other school information useful for the current day. It defaults off.

## Scheduling and worker ownership

`MorningBriefingScheduler` has no timer and creates no thread. The daemon
attaches it to the existing `AmbientDigestWorker`, whose single stoppable
thread wakes at most every 30 seconds. Ambient digest cadence remains
`passive_digest_interval_min`; a morning check does not trigger a digest.

The shared worker runs when passive capture is enabled, the morning briefing
is enabled, or both. With both features off there is no ambient worker. A
live passive-capture switch can stop the worker only when the morning feature
does not still need it. Daemon shutdown always stops the shared worker before
the database and TTS engine close.

An empty School branch produces no model call and no speech. The scheduler
remembers that empty result for the rest of the process-local day so the
shared worker does not repeatedly reopen the graph on every wake.

## Once-per-day gate

The configured trigger is `morning_briefing_time` in local 24-hour `HH:MM`
form. Before that time, a check is a no-op. At or after the trigger, the
scheduler reads `morning_briefing.last_delivered_local_date` from the
SQLite `app_state` table. A value equal to today's local date suppresses all
work.

Only today's briefing can be generated. A stored date from several days ago
causes one briefing for today, never one replay per missed day. The gate is
written only after `tts.speak()` accepts the briefing into its queue, so a
generation or queue failure remains eligible for retry. A generated briefing
that must defer is cached in memory for that local day rather than regenerated
on every worker wake.

## Conversation safety

The daemon's availability check requires all of these immediately before
generation and again immediately before queueing speech:

- TTS is enabled and not speaking.
- Runtime phase is idle.
- No text or voice query holds the shared query lock.
- Voice activity detection does not report active speech.
- The listener is not in conversation mode, a hot follow-up window, or
  command capture.

If any condition is false, the scheduler defers and does not advance the
persisted gate.

## Generation

The School snapshot is sent as fenced untrusted data to a CHAT-tier direct
call. The static system prompt begins with `build_reply_prompt_prefix(cfg)`,
so the same persona and voice-language decision as an ordinary reply owns all
phrasing. Briefing-specific rules request plain spoken sentences under one
minute and forbid invented or silently corrected dates. When the configured
voice does not name a language, the model treats School data as the user's
content and follows its predominant language. No sentence template or
language-specific fragments are assembled in Python.

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `morning_briefing_enabled` | bool | `false` | Enable one spoken School summary per local day |
| `morning_briefing_time` | str | `"07:00"` | Earliest local time at which today's summary may be queued |

An invalid time read from configuration falls back to `07:00` with a CLI
warning. Both keys appear in the `🎓 School` settings category.
