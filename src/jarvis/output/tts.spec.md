# TTS engine specification

## Engine selection

`create_tts_engine(engine=cfg.tts_engine, ...)` in `tts.py` is the one place
that turns `tts_engine` (`"piper"` | `"chatterbox"` | `"kokoro"`) into a
concrete engine instance. `debug_log` records which engine was selected.
Every engine shares one interface (`start`, `stop`, `speak`, `end_of_reply`,
`interrupt`, `is_speaking`, `get_last_spoken_text`) and one queue-of-`Utterance`
worker-thread shape, so the rest of the daemon (the reply engine, the
listener's echo detection, the face/visualizer waveform feed) never branches
on which engine is running. An unrecognised value falls back to Piper, the
default.

## Kokoro: a sidecar engine

Kokoro is vendored, AGPL-3.0-licensed code (`jarvis.output.vendor.kokoro_backtalk`,
from `github.com/jaredrhod/backtalk` — see `THIRD_PARTY_NOTICES.md`). Unlike
Piper and Chatterbox, its synthesis code does not run inside the daemon
process at all: it runs in its own subprocess, so the AGPL code and its
PyTorch dependency (the `kokoro` package) stay on their own side of a
process boundary, the same separable shape the Face/visualizer view already
has over the network. `tests/test_kokoro_process_boundary.py` enforces with
a source scan that nothing outside `jarvis.output.vendor.kokoro_backtalk.py`
and the sidecar entry point below imports either.

### Architecture

| Piece | Role |
|---|---|
| `jarvis.output.tts.KokoroTTS` | The engine Jarvis's TTS interface talks to: queue, worker thread, playback, interruption — the same shape as `PiperTTS`. Holds one `KokoroSidecarClient`. |
| `jarvis.output.kokoro_sidecar_client.KokoroSidecarClient` | The main-process side of the boundary. Launches the sidecar subprocess lazily, writes one synthesis request per utterance, and yields the PCM chunks the sidecar streams back. Never imports `kokoro_backtalk` or the `kokoro` package. |
| `jarvis.output.vendor.kokoro_sidecar` | The subprocess entry point (`python -m jarvis.output.vendor.kokoro_sidecar`). The only module that imports `kokoro_backtalk`. Reads requests from stdin, writes responses to stdout, and never exits on a synthesis failure, only on a `shutdown` command or stdin EOF. |

### Protocol

Newline-delimited JSON, one message per line, over the subprocess's own
stdin/stdout pipes (not a network socket: this is a local, per-process
pipe, torn down with the subprocess).

Request (client → sidecar):

```json
{"cmd": "synthesize", "id": 1, "text": "...", "voice": "bm_lewis", "speed": 1.0}
{"cmd": "shutdown"}
```

Response (sidecar → client), zero or more per request `id`:

```json
{"type": "ready"}
{"type": "chunk", "id": 1, "pcm_b64": "..."}
{"type": "end", "id": 1}
{"type": "error", "id": 1, "message": "..."}
```

`pcm_b64` is one Kokoro-yielded int16 PCM chunk, base64-encoded. Chunks are
emitted as Kokoro produces them rather than batched into one message at the
end of the utterance, so a failure surfaces as soon as the sidecar reports
it and the client's playback pipeline sees the same per-chunk shape it
would from an in-process generator. One utterance is synthesised per
request; the reply engine's own sentence-by-sentence queuing (each finished
sentence is spoken as soon as it is written, not after the whole reply) is
what keeps speech starting promptly, and is unaffected by the process
boundary — `KokoroTTS`'s worker thread still handles exactly one utterance,
and therefore exactly one sidecar round trip, at a time.

### Lifecycle

| Event | Effect |
|---|---|
| `KokoroTTS` constructed | Holds a `KokoroSidecarClient`; nothing is launched yet. |
| `KokoroTTS.start()` | Starts the worker thread only. The sidecar subprocess is still not launched. |
| First utterance actually spoken | `KokoroSidecarClient.synthesize()` launches the sidecar (`subprocess.Popen`), reads its `{"type": "ready"}` line, and only then sends the synthesis request. Model loading and the Kokoro model download stay inside the sidecar and lazy within it too: the subprocess launching is not itself a model load. |
| Later utterances | Reuse the same running subprocess; no relaunch while it is alive. |
| The subprocess dies or its pipe breaks | `KokoroSidecarClient` drops its process reference and raises `KokoroSidecarError`; the next utterance's `synthesize()` call relaunches a fresh subprocess. |
| `KokoroTTS.stop()` | Stops the worker thread, then asks the sidecar to exit (`{"cmd": "shutdown"}`) and waits for it, killing it if it does not exit within 3 seconds. |

### Error handling

A missing `kokoro` package, a failed model download, or any other synthesis
exception inside the sidecar is caught there and reported as
`{"type": "error", ...}`; the sidecar process itself keeps running. A
subprocess crash, a broken pipe, or a sidecar that never reports ready
raises `KokoroSidecarError` in the client. `KokoroTTS._speak_once` catches
`KokoroSidecarError` the same way `PiperTTS` handles a failed voice
download: `debug_log`, a printed warning, and the utterance is skipped
rather than spoken — no raw subprocess or pipe exception reaches the reply
engine or the user.
