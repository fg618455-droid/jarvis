# Runtime Specification

Live state, per-turn timings, and the event bus that carries both to
whoever is watching.

## Design Principles

1. **The ruler must not weigh.** Every measurement is a `perf_counter()`
   reading and a list append. No logging, no formatting, no I/O until the
   turn is over. Latency is what this measures, so it may not add any.
2. **Never in the way.** Publishing an event, writing the journal, and
   updating state all fail quietly. A full disk or a page nobody is reading
   must not be able to stop a reply.
3. **A turn belongs to its thread.** A typed turn from the control centre
   and a spoken turn can be in flight at once without mixing their stages.
4. **Absent instrumentation is not an error.** `stage()`, `mark()`, and
   `record_tool()` do nothing outside a turn, so deep call sites can be
   measured without every caller knowing where it was reached from.

## Phases

The phase says what the assistant is doing, in the order a turn passes
through it.

| Phase | Meaning | Set by |
|---|---|---|
| `starting` | Models still loading | daemon start |
| `idle` | Waiting for the wake word | daemon once the listener runs; every stage hands back here |
| `capturing` | Voice detected, recording the utterance | the listener's audio loop |
| `transcribing` | Whisper running | `_transcribe_and_route` |
| `thinking` | Reply engine running | `_dispatch_query` |
| `tool` | A tool is executing | the tool registry |
| `speaking` | Synthesised speech is playing | the TTS engine, at first sound |
| `dictating` | Hold-to-dictate has the microphone | the dictation engine |

`set_phase_if(expected, phase)` moves only when the assistant is still in
`expected`, so a stage can hand the phase back without overwriting one a
later stage has already claimed. Speech is the last stage of a turn, so the
TTS engine owns the return to `idle` and does it however the playback
ended, cut short included.

## A turn

The clock starts the moment speaking stopped, because that is when the wait
the user feels begins. It ends at the first sound out, not at the finished
reply text: synthesis is part of the wait.

| Stage | Measured around |
|---|---|
| `stt` | The transcription call, whichever backend served it |
| `tool_routing` | `select_tools` |
| `planner` | `plan_query`, when the planner is enabled |
| `recall` | Diary and graph enrichment together |
| `llm` | Each chat call, once per agentic turn |
| `tts_synth` | Handing the reply to speech until the first sample plays |

Tool calls are recorded separately from stages, with their name, duration,
outcome, and whether the security gate refused them.

A turn that never reaches the reply engine, because there was no wake word
or the utterance was noise, is abandoned rather than filed.

## History and journal

The last 50 finished turns are held in memory for the control centre to
read at once. Finished turns are also appended to
`<db directory>/turns.jsonl`, rotated to `.1` at 5 MB, so a comparison can
span restarts. The journal is opt-in per process: the daemon points the
recorder at a path, and nothing is written until it does.

## Discarded utterances

An utterance thrown away is counted under the reason it was thrown away
for, because a silent discard is the usual cause of "it ignored me".

| Reason | Cause |
|---|---|
| `too_short` | Below `whisper_min_audio_duration` |
| `no_speech` | The recogniser produced no text, usually Whisper's own VAD |
| `language_probability` | Below `whisper_min_language_probability` |
| `repetitive` | Caught by the repetition guard |
| `stt_error` | Transcription raised |

## Event bus

Subscribers receive `{"kind": ..., "data": {...}}`. A subscriber that falls
more than 256 events behind loses its oldest ones: publishing is bounded in
time and memory whatever the watcher does.

| Kind | Published when |
|---|---|
| `phase` | The phase changes |
| `stage` | A turn reaches a stage, for progress before it ends |
| `turn` | A turn is filed |
| `discarded` | An utterance is thrown away |
| `passive` | The passive-capture switch flips, or a line is written down |
| `conversation` | A wake-word-free conversation starts or ends |
| `crew` | A reading of the NAS-hosted agent crew is taken |
| `error` | An error is recorded |

Every kind but `crew` comes from the voice path. `crew` is published by the
control centre's own poller, which is the one publisher the assistant does
not drive. It rides this bus rather than a channel of its own because every
page is already listening here, and a second stream would be a second
connection to keep alive for the same purpose. See
`../webui/webui.spec.md`.

## Passive capture

A `passive` block carries whether the record is being written, how many
lines and digests this session produced, and when the last line was written,
so the control centre's header can show at all times whether the room is
being written down. See `../listening/passive_capture.spec.md`.

## Conversation mode

A `conversation` block carries whether a wake-word-free conversation is
running. The listener's state manager owns it and publishes both
transitions; the runtime holds the copy an interface reads. It is pushed
rather than polled because the conversation also ends without anyone
clicking, on the intent judge's `stop` decision. See
`../listening/listening.spec.md`.
