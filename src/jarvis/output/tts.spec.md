# TTS engine specification

## Engine selection

`create_tts_engine(engine=cfg.tts_engine, ...)` in `tts.py` is the one place
that turns `tts_engine` (`"piper"` | `"chatterbox"` | `"kokoro"` | `"cloud"`)
into a concrete engine instance. `debug_log` records which engine was selected.
Every engine shares one interface (`start`, `stop`, `speak`, `end_of_reply`,
`interrupt`, `is_speaking`, `get_last_spoken_text`) and one queue-of-`Utterance`
worker-thread shape, so the rest of the daemon (the reply engine, the
listener's echo detection, the face/visualizer waveform feed) never branches
on which engine is running. An unrecognised value falls back to Piper, the
default.

The optional morning School briefing enters through the same `speak` queue.
Its scheduler checks that TTS, listening, conversation, and query state are
idle immediately before queueing; a busy state defers rather than interrupts.

The default is `"piper"`. Cloud speech is opt-in: an absent, old, or fresh
configuration cannot select it and cannot send text off the computer. TTS is
an output-only path. Wake-word detection and passive or always-on microphone
paths remain local.

## Cloud: ordered providers with a local final stage

`CloudTTS` owns the same queue, worker thread, playback stream, per-utterance
callbacks, interruption events and loopback state as the local engines. For
each utterance it tries configured, enabled and healthy cloud providers in
list order. The first provider to return usable audio supplies the utterance.
Quota exhaustion, rate limiting, authentication failure, timeout, network
failure, unavailable audio and an empty response fall through. Selection and
every fall-through are recorded with `debug_log` without credentials or
spoken text.

The final candidate is always a local engine constructed by
`create_tts_engine` from `tts_local_fallback_engine`. The setting accepts
`"piper"`, `"chatterbox"` or `"kokoro"` and defaults to `"piper"`; an invalid
value also resolves to Piper. It cannot select `"cloud"`, so the chain cannot
recurse or configure away its local end. When that local engine fails,
`CloudTTS` logs and prints a warning and skips the utterance. No raw speech
exception reaches the reply engine.

An interrupt sets the cloud engine's cancellation event, aborts an active
audio stream and interrupts the local engine. The provider loop checks that
event before synthesis, between yielded chunks, after synthesis and before
every fall-through. An interrupted attempt never starts the next provider.

### Provider interface and audio

One vendor client implements `CloudTTSProvider.synthesise(text, voice_id,
model, timeout_sec, cancelled)` and yields `TTSAudioChunk` objects. The
cancellation event gives a network-free fake and a real streaming client the
same observable interruption contract. A client raises one of these typed
failures when it cannot provide audio:

| Failure | Meaning |
|---|---|
| `TTSRateLimited` | Temporary request-rate limit, optionally with `retry_after` seconds |
| `TTSQuotaExhausted` | Allowance exhausted, optionally with an absolute UTC `reset_at` timestamp |
| `TTSAuthenticationError` | Missing, rejected or expired credential |
| `TTSProviderTimeout` | The configured provider deadline expired |
| `TTSNetworkError` | Transport or remote connectivity failure |
| `TTSProviderUnavailable` | Client unavailable or response audio unsupported or malformed |

Chunks carry either signed 16-bit little-endian mono PCM and its sample rate,
or one complete uncompressed mono 16-bit PCM WAV response. WAV parsing uses
the standard-library `wave` module. MP3 and other compressed formats are not
accepted or decoded. A provider whose plan cannot return PCM or WAV raises
`TTSProviderUnavailable`, allowing the chain to continue without an audio
decoder dependency.

Provider clients yield chunks as the response arrives. `CloudTTS` checks for
interruptions between chunks, then joins one successful attempt for playback
through the same sounddevice callback and visualiser waveform feed used by
Kokoro. Keeping attempt audio separate prevents a partly failed provider from
being followed by a duplicate rendition of the utterance.

Fish Audio and ElevenLabs are built-in plain-HTTP clients using the pinned
`requests` dependency. No vendor SDK runs in the daemon. An unknown provider
identifier reports `TTSProviderUnavailable` and falls through to the next
provider or the local final stage.

### Fish Audio and ElevenLabs wire contracts

Both clients request 24 kHz, signed 16-bit little-endian mono PCM and expose
each complete sample-aligned block as `TTSAudioChunk.pcm16`. HTTP response
chunks are transport framing, not sample framing, so a client carries an odd
trailing byte into the next response chunk. An odd final byte, empty body,
JSON body in a successful response, timeout during streaming or malformed
chunk is a failure. `CloudTTS` does not play any part of an attempt until that
attempt has completed successfully.

Fish Audio uses `POST https://api.fish.audio/v1/tts`, authenticates with
`Authorization: Bearer <key>`, places the configured model in the `model`
header and sends `text`, `reference_id`, `format: "pcm"` and
`sample_rate: 24000` as JSON. A successful response is raw PCM over HTTP
chunked transfer encoding. Fish Audio documents WAV and PCM, including PCM
on its free developer model, without an output-format plan restriction. Its
API reference states that an unrecognised model header falls back to the
service default rather than returning a model error; it does not define a
distinct unknown-voice response.

ElevenLabs uses
`POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream`,
authenticates with `xi-api-key`, sends `text` and `model_id` as JSON and sets
the `output_format=pcm_24000` query parameter. A successful response is raw
audio bytes over HTTP chunked transfer encoding. ElevenLabs documents 44.1
kHz PCM as requiring its Pro tier, but does not state a plan restriction for
24 kHz PCM. A `feature_not_available`, `subscription_required` or
`invalid_output_format` response reports `TTSProviderUnavailable`, so a plan
that cannot supply the required PCM format falls through safely.

| Vendor response | Provider failure | Timing carried |
|---|---|---|
| Fish Audio HTTP 401 | `TTSAuthenticationError` | None |
| Fish Audio HTTP 402 | `TTSQuotaExhausted` | No reset is documented |
| Fish Audio HTTP 429 | `TTSRateLimited` | `Retry-After` seconds or HTTP date when present |
| Fish Audio unknown voice or other non-success | `TTSProviderError` | None |
| ElevenLabs HTTP 401 or `authentication_error` | `TTSAuthenticationError` | None |
| ElevenLabs HTTP 402, `payment_required`, `insufficient_credits` or legacy `quota_exceeded` | `TTSQuotaExhausted` | No reset is documented |
| ElevenLabs HTTP 429 or `rate_limit_error` | `TTSRateLimited` | `Retry-After` seconds or HTTP date when present |
| ElevenLabs PCM plan or format rejection | `TTSProviderUnavailable` | None |
| ElevenLabs unknown voice, unknown model or other non-success | `TTSProviderError` | None |
| Missing credential or voice id | `TTSProviderUnavailable` | None, and no request is sent |
| Request timeout, connection failure or interrupted response | `TTSProviderError` | None |

Neither documented TTS response exposes a quota reset timestamp. An exhausted
quota therefore uses the state store's midnight UTC fallback. Exception and
log messages contain only safe provider and failure labels, never an endpoint,
credential, voice id or spoken text.

### Provider health and cooldown state

`TTSProviderStateStore` writes `~/.jarvis/tts_provider_state.json` atomically
with mode `0o600` where the platform supports POSIX permissions. Keys are
hashes of non-secret provider identity fields. Values contain only hit,
failure, rate-limit and block counters plus the last safe error class. The
file never contains a URL, credential, environment variable name, voice id,
model name or spoken text.

| Failure | Block |
|---|---|
| Rate limit with `retry_after` | Exactly the stated duration |
| Rate limit without a duration | 60 seconds, then 300 seconds, then 900 seconds |
| Quota exhaustion with `reset_at` | Until the stated reset |
| Quota exhaustion without a reset | Until midnight UTC |
| Authentication failure | Invalid for the process lifetime only |
| Timeout, network, unavailable or empty audio | Recorded, then the next utterance may retry |

Rate-limit and quota blocks persist across process restarts. Authentication
invalidation is process-local and is not written as a persisted block, so a
restart can retry a corrected environment credential.

### Configuration

`tts_cloud_providers` is an ordered list. The default order is Fish Audio,
ElevenLabs and then the mandatory local Piper stage. Malformed entries are
ignored. Each valid entry has `name`, `provider`, `api_key_env`, `voice_id`,
`model`, `enabled` and `timeout_sec`. Credentials never appear in this list.
The value of `api_key_env` names an environment variable; its value is read
lazily when the provider client is first built and is never copied into
configuration, logged or represented by the engine. The whole chain remains
off until `tts_engine` is explicitly set to `"cloud"`.

Both settings interfaces expose this list through the shared metadata
registry. The control centre renders ordered provider cards and the Qt window
renders typed table columns, including move controls. They store only the
environment-variable name, never the credential value.

```json
{
  "tts_engine": "cloud",
  "tts_cloud_providers": [
    {
      "name": "Fish Audio",
      "provider": "fish_audio",
      "api_key_env": "FISH_AUDIO_API_KEY",
      "voice_id": "fish-voice-id",
      "model": "s2.1-pro-free",
      "enabled": true,
      "timeout_sec": 10.0
    },
    {
      "name": "ElevenLabs",
      "provider": "elevenlabs",
      "api_key_env": "ELEVENLABS_API_KEY",
      "voice_id": "elevenlabs-voice-id",
      "model": "eleven_multilingual_v2",
      "enabled": true,
      "timeout_sec": 10.0
    }
  ],
  "tts_local_fallback_engine": "piper"
}
```

Voice ids are opaque and specific to one provider. A recognisable voice across
both cloud stages comes from manually cloning the same human reference
recording once at Fish Audio and once at ElevenLabs, then placing the two
resulting ids in their respective entries. There is no shared voice id and no
voice-cloning upload flow in Jarvis.

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
