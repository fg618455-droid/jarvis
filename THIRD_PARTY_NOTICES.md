# Third-Party Notices

Jarvis's own code is licensed under the terms in `LICENSE`. The component
listed below is vendored from another project under its own licence, which
continues to apply to those files regardless of the licence on the rest of
this repository.

## backtalk (AGPL-3.0-or-later)

- **Source:** https://github.com/jaredrhod/backtalk
- **Author:** Jared Rhodenizer, Copyright (C) 2026
- **Licence:** GNU Affero General Public License, version 3 or later. Full
  text: https://www.gnu.org/licenses/agpl-3.0.html
- **Location in this repository:** `src/jarvis/output/vendor/kokoro_backtalk.py`,
  run only inside the sidecar subprocess entry point
  `src/jarvis/output/vendor/kokoro_sidecar.py` (also in that folder, but
  original Jarvis code, not vendored from backtalk).
- **What is vendored:** the Kokoro half of `backtalk/mouth.py` — the
  espeak-ng discovery, the Kokoro pipeline loader, the sentence splitter,
  and the streaming synthesis call. backtalk's ElevenLabs cloud path and its
  keychain lookup are **not** vendored: Jarvis's voice pipeline is
  offline-first by principle and does not add a cloud TTS vendor.
  `backtalk/ears.py` (its faster-whisper speech-to-text path) is also not
  vendored — Jarvis's own `faster-whisper` + WebRTC VAD listener already
  covers the same ground with configurable endpointing, so importing it
  would add a second, redundant speech-recognition path rather than new
  capability.
- **What changed:** the vendored functions take their voice and speed as
  arguments instead of reading backtalk's global `CFG`, and log through
  `jarvis.debug.debug_log` instead of backtalk's own logger, so
  `kokoro_sidecar.py` can call them directly. The synthesis algorithm, the
  sentence splitting, and the espeak-ng path discovery are otherwise
  backtalk's own code, and the AGPL header is unmodified.
- **Process boundary:** `kokoro_backtalk.py` and the `kokoro` package are
  imported only by `kokoro_sidecar.py`, which runs as its own subprocess
  (`python -m jarvis.output.vendor.kokoro_sidecar`), launched lazily by
  `jarvis.output.kokoro_sidecar_client.KokoroSidecarClient` the first time
  Kokoro speech is actually requested. Nothing in the main daemon process,
  including `jarvis.output.tts.KokoroTTS`, imports either — see "Licence
  boundary" below. `tests/test_kokoro_process_boundary.py` enforces this
  with a source scan.

## Licence boundary

The vendored component is reached from the main daemon process only over a
local interface, not by direct import, and is a separable program the AGPL's
own terms already anticipate this way:

- The **Kokoro engine**'s AGPL code (`kokoro_backtalk.py`) and its PyTorch
  dependency (`kokoro`) run only inside the sidecar subprocess
  (`kokoro_sidecar.py`), talked to over a stdin/stdout pipe by
  `KokoroSidecarClient`. The main daemon process, including
  `jarvis.output.tts.KokoroTTS`, never imports `kokoro_backtalk.py` or the
  `kokoro` package; the two processes exchange only synthesis requests and
  PCM audio as newline-delimited JSON messages.

These are therefore separate programs communicating over a local interface
rather than one linked combination, the shape the AGPL's own FAQ describes as
separate works rather than a derivative combination. Reaching the sidecar
over its local pipe is "conveying" under AGPL section 13, so a user running
the Kokoro sidecar can request `backtalk`'s source from the URL above. It
does not require, and does not trigger, relicensing any other file in this
repository. Piper and Chatterbox remain unaffected, since they carry no AGPL
code at all.
