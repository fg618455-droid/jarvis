# Passive Capture Specification

Wake-word mode already transcribes every word spoken near the microphone.
Everything that is not addressed to the assistant falls out of the rolling
transcript buffer two minutes later and is gone. Passive capture is the
switch that keeps it instead: the room's speech is written down, and what is
worth remembering is folded into the same memory the assistant already has.

Covers `src/jarvis/listening/passive_capture.py` (the record) and
`src/jarvis/memory/ambient.py` (the digest). The hygiene rules the digest
prompt must obey live in `src/jarvis/memory/summariser.spec.md`.

## Design Principles

1. **Off unless asked.** The switch defaults to off and nothing about the
   feature runs until it is turned on. A microphone that writes down
   everything is not something anyone should discover by accident.
2. **Nothing new is heard.** Passive capture adds no recording, no second
   stream, and no change to the audio path. It keeps text the recogniser
   already produced. **No audio is ever written to disk.**
3. **Local, like everything else.** The record lives in the assistant's own
   SQLite file next to the diary. Nothing is uploaded, and the digest runs
   on whichever model backend the user has already configured.
4. **Overheard is not said to me.** Ambient speech may be a housemate, a
   television, or a phone call. It is recorded and remembered as *overheard*,
   never as something the user told the assistant.
5. **Visible while it happens.** Whether the record is being written is shown
   in the control centre at all times, on every view, not buried in settings.
6. **Deletable, and honest about what deletion reaches.** A line can be
   deleted, a day can be deleted, the whole record can be deleted. What has
   already been folded into the diary or the graph is a separate record with
   its own delete path, and the interface says so rather than implying a
   single button erases every trace.

## What is captured

The rolling transcript buffer is the source. A segment reaches it only after
the recogniser's own filters have passed it: minimum audio duration, Whisper's
VAD and `no_speech_prob`, the confidence floor, language probability, and the
repetition guard. Everything those discard (and count under
`runtime.spec.md` → Discarded utterances) is noise, and passive capture does
not resurrect it.

A segment is handed to the record **when it is evicted from the rolling
buffer**, not when it enters. By then its text is final: echo salvage has
already rewritten it in place, and the listener has already marked whether it
became a query. One hand-off point, no updates, and no second copy of the
filtering rules.

| Consequence | Behaviour |
|---|---|
| Lag | A line appears in the record up to `transcript_buffer_duration_sec` (default 120 s) after it was spoken |
| Shutdown | The listener flushes whatever the buffer still holds when it stops |
| Turning the switch off | Whatever is still in the buffer is dropped rather than written. The switch is read at hand-off, so switching off looks backwards as far as the buffer reaches |
| Clearing the record | Also clears the rolling buffer, so a delete is not undone two minutes later |

Two kinds of segment are never written down:

- **Echo.** A segment the echo check rejected, or one salvaged out of an
  echo-and-speech chunk, carries the assistant's own voice. The listener
  marks it on the buffer; the record skips it. What the assistant said is
  already in the diary, said properly.
- **Too short to mean anything.** Below `passive_capture_min_words` (default
  3). "Yeah", "mhm", and "one sec" would otherwise dominate the record and
  the digest.

A segment that *was* addressed to the assistant is written down like any
other, flagged `addressed`, because the transcript view is meant to be a
readable account of what was said in the room rather than an account of the
half of it the assistant ignored. The digest skips those lines: they are
already in the diary through the normal path.

## Storage

One table, in the same database as the diary, created by the same schema
script:

```sql
CREATE TABLE IF NOT EXISTS passive_transcripts (
  id          INTEGER PRIMARY KEY,
  ts_utc      TEXT NOT NULL,     -- when the speech started, ISO 8601 UTC
  date_utc    TEXT NOT NULL,     -- YYYY-MM-DD, the unit a day is deleted by
  duration_sec REAL,
  text        TEXT NOT NULL,
  language    TEXT,              -- what the recogniser identified, ISO-639-1
  addressed   INTEGER NOT NULL DEFAULT 0,  -- became a query to the assistant
  digested    INTEGER NOT NULL DEFAULT 0,  -- already folded into memory
  source_app  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passive_by_date ON passive_transcripts(date_utc);
```

Text is stored as it was heard. Redaction happens on the way to the model,
exactly as it does for diary chunks, so that what the user reads back in the
control centre is what was actually said while the model never sees a
credential that was read aloud.

There is no FTS index and no embedding. The record is a log to read and
delete, not a retrieval surface; only digests reach memory.

**Retention.** Lines older than `passive_capture_retention_days` (default 30)
are deleted on daemon start and once a day while it runs. `0` means keep
until deleted by hand. Retention is enforced whether or not the switch is
currently on, so turning capture off does not strand an old record forever.

## The digest

Raw ambient lines never enter a reply prompt. They reach memory only through
a digest, so that a day of room chatter costs the reply path nothing and
cannot flood it.

The shared `AmbientDigestWorker` wakes at most every 30 seconds because it
also hosts the timer-free morning School briefing gate. Ambient processing
still runs only every `passive_digest_interval_min` (default 15), only while
passive capture is enabled, and only when there is anything undigested and
not `addressed`:

1. Takes the undigested lines for one UTC day, oldest day first, capped at
   `passive_digest_max_lines` (default 120) per pass so a backlog is worked
   through in bounded steps rather than one enormous prompt.
2. Redacts them (`utils/redact.py`), and fences them as untrusted data with
   the same markers the web-search and diary-rewrite paths use. Ambient
   speech is the most obvious injection surface the assistant has: anything
   said aloud near the microphone, by anyone, including a television, ends
   up in a prompt.
3. Asks the chat tier for a short digest under the ambient hygiene rules in
   `summariser.spec.md`. **An empty digest is the ordinary answer** — most
   speech in a room carries nothing worth keeping.
4. If the digest is non-empty, appends it as one chunk to the day's diary row
   through `update_daily_conversation_summary`, then runs graph extraction
   over the updated summary through the same helper the diary flush uses.
   Nothing new is plumbed into the graph; existing deduplication absorbs
   repeats.
5. Marks the lines `digested` whether or not the digest was empty, so an
   uneventful hour is not re-read on every pass.

Lines are marked only after the write succeeds. A failed LLM call, a failed
diary write, or a crash mid-pass leaves them undigested and the next pass
picks them up again. The worker never raises into the audio path.

**Delete during a pass.** A line deleted while it is being digested is gone
from the record; whatever the model already made of it may still land in the
diary. The interface's deletion notice covers this.

## Runtime and the control centre

Runtime state carries a `passive` block: whether the switch is on, how many
lines have been written this session, how many digests have been produced,
and when the last line was written. It is published on the event bus as kind
`passive` whenever the switch flips or a line is written, so the header does
not poll.

| Where | What is shown |
|---|---|
| Header, every view | A recording indicator sitting next to the phase: a filled dot and "recording everything" while the switch is on, nothing at all while it is off |
| Passive record view | Its own destination: the switch, the state and the undigested count in the frame, then lines grouped by day with their time and text, a named mark on the ones addressed to the assistant, and per-line, per-day, and whole-record delete buttons |
| Settings | The switch and its three companions, rendered from the config metadata registry like every other key |

The record is a destination of its own rather than a section of another
view. It is a privacy surface with its own switch and its own delete paths,
and it grows without limit: an account of every word spoken in the room
cannot share a page with something a reader is meant to scroll past it to
reach.

The switch has to be reachable while the daemon is running, so a config file
the daemon read at start is not enough on its own. `POST /api/passive/enabled`
does both halves: it flips the running recorder through a module-level switch
(the shape `conversation_mode.py` already uses for the same problem) and
writes the config key so the choice survives a restart. Turning it **on**
through the interface first states which model backend will see the ambient
text, because "local" depends on what the user pointed `llm_provider` at.

### API

| Route | Serves |
|---|---|
| `GET /api/passive` | Lines, newest first, `date` and `limit` filters, plus the switch state and the undigested count |
| `POST /api/passive/enabled` | Flip the running recorder and persist the choice |
| `DELETE /api/passive/<id>` | One line |
| `DELETE /api/passive?date=YYYY-MM-DD` | One day |
| `DELETE /api/passive?all=1` | The whole record, and the rolling buffer with it |

Deletes go through the control centre's existing write guards: the
`X-Jarvis-UI` header and, off loopback, the token.

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `passive_capture_enabled` | bool | `false` | Write down everything heard, not only what is addressed to the assistant |
| `passive_capture_retention_days` | int | `30` | Days a line is kept before it is deleted automatically. `0` keeps it until deleted by hand |
| `passive_capture_min_words` | int | `3` | Utterances shorter than this are not written down |
| `passive_digest_interval_min` | float | `15` | How often ambient speech is folded into memory |
| `passive_digest_max_lines` | int | `120` | Lines per digest pass |

All five sit in their own metadata category, `("passive", "📝 Passive Capture")`, ordered after memory, so the settings form groups them where a reader looking for "does this thing write down my kitchen" will find them.

## Failure behaviour

Passive capture is a bystander to the voice path and fails like one.

| Failure | Behaviour |
|---|---|
| The record cannot be written | Logged through `debug_log`, counted as an error in runtime state, the utterance carries on to the intent path untouched |
| The digest model is unreachable | The pass ends, lines stay undigested, the next pass tries again |
| The digest returns nothing usable | Lines are marked digested, nothing is written to the diary |
| The database is locked | WAL handles the overlap with the reply path; a genuine failure is logged and retried on the next pass |

Nothing in this feature may block the audio loop, delay a reply, or stop the
daemon from starting.

The worker exists while passive capture or the morning School briefing is
enabled. Switching passive capture off stops it only when the morning feature
does not need it. Both features share this one thread and daemon shutdown
stops it once.

## Regression guards

| Test | Location | Guards |
|---|---|---|
| `test_nothing_is_written_while_the_switch_is_off` | `tests/test_passive_capture.py` | Default-off, and the switch read at hand-off |
| `test_evicted_segments_reach_the_record` | `tests/test_passive_capture.py` | The hand-off point and its ordering |
| `test_echo_segments_are_never_written` | `tests/test_passive_capture.py` | The assistant's own voice stays out |
| `test_short_utterances_are_dropped` | `tests/test_passive_capture.py` | `passive_capture_min_words` |
| `test_switching_off_drops_the_live_buffer` | `tests/test_passive_capture.py` | No write-behind after the switch flips |
| `test_clearing_the_record_clears_the_buffer` | `tests/test_passive_capture.py` | A delete is not undone by the next eviction |
| `test_retention_deletes_old_lines` | `tests/test_passive_capture.py` | `passive_capture_retention_days`, including `0` |
| `test_a_failed_write_does_not_break_the_utterance` | `tests/test_passive_capture.py` | Fail-open into the audio path |
| `test_addressed_lines_are_not_digested` | `tests/test_ambient_digest.py` | No double-recording of what the diary already holds |
| `test_lines_stay_undigested_when_the_model_fails` | `tests/test_ambient_digest.py` | Retry semantics |
| `test_empty_digest_marks_lines_without_writing_a_diary_row` | `tests/test_ambient_digest.py` | An uneventful hour costs one call, once |
| `test_ambient_text_is_fenced_and_redacted` | `tests/test_ambient_digest.py` | Injection fence and credential scrub |
| `test_digest_attributes_content_as_overheard` | `evals/test_ambient_digest_hygiene.py` | Provenance rule (`summariser.spec.md`) |
| `test_digest_returns_nothing_for_small_talk` | `evals/test_ambient_digest_hygiene.py` | Empty is the ordinary answer |
| `test_digest_ignores_recited_and_broadcast_speech` | `evals/test_ambient_digest_hygiene.py` | Television and podcasts are not the household's facts |
| `TestPassiveApi` | `tests/test_webui_passive_api.py` | Listing, the three deletes, write guards, switch persistence |

## Relationship to other systems

- **Listening** (`listening.spec.md`): passive capture consumes the rolling
  transcript buffer's evictions. It changes no listening decision, no gating,
  and no timing.
- **Summariser** (`summariser.spec.md`): owns the digest prompt contract and
  its hygiene rules, alongside the diary summariser's.
- **Runtime** (`runtime.spec.md`): the `passive` state block and event kind.
- **Control centre** (`webui.spec.md`): the header indicator, the Passive
  record view, and the API routes.
