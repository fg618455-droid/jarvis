# Third-Party Notices

Jarvis's own code is licensed under the terms in `LICENSE`. The components
listed below are vendored from other projects under their own licences,
which continue to apply to those files regardless of the licence on the
rest of this repository.

## ai-visualizer (AGPL-3.0-or-later)

- **Source:** https://github.com/jaredrhod/ai-visualizer
- **Author:** Jared Rhodenizer, Copyright (C) 2026
- **Licence:** GNU Affero General Public License, version 3 or later. Full
  text: https://www.gnu.org/licenses/agpl-3.0.html
- **Location in this repository:** `src/jarvis/webui/visualizer/vendor/`
- **What is vendored:** the face gallery (`index.html`), the shared bus
  polling and rendering runtime (`core.js`), the four bundled faces
  (`faces/board`, `faces/radial`, `faces/rain`, `faces/neural`), and their
  static assets (`assets/face.png`, `assets/thinking.wav`).
- **What changed:** `core.js` and the gallery `index.html` poll
  `/api/visualizer/state` and `/api/visualizer/config` instead of
  ai-visualizer's own `/state` and `/config`, because the face is served by
  Jarvis's own control centre process rather than ai-visualizer's stdlib
  HTTP server. No other line was changed; the AGPL header is unmodified in
  every file. The face is reached over Jarvis's control centre, which the
  AGPL treats as conveying under section 13 (the network-use clause) — a
  user who can browse to the Face view can request the corresponding source
  from the URL above.
- **Font:** the bundled `assets/VT323-Regular.ttf` typeface is Peter Hull's
  VT323, under the SIL Open Font License 1.1 (`assets/VT323-OFL.txt`), not
  the AGPL — a separate licence from the surrounding ai-visualizer code.

## backtalk (AGPL-3.0-or-later)

- **Source:** https://github.com/jaredrhod/backtalk
- **Author:** Jared Rhodenizer, Copyright (C) 2026
- **Licence:** GNU Affero General Public License, version 3 or later. Full
  text: https://www.gnu.org/licenses/agpl-3.0.html
- **Location in this repository:** `src/jarvis/output/vendor/kokoro_backtalk.py`
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
  `jarvis.output.tts.KokoroTTS` can call them directly. The synthesis
  algorithm, the sentence splitting, and the espeak-ng path discovery are
  otherwise backtalk's own code, and the AGPL header is unmodified.

## Licence boundary

The ai-visualizer face is served as its own set of static files and two
small JSON endpoints; nothing in `src/jarvis/webui/visualizer/vendor/`
imports Jarvis's own modules; the only Jarvis code that touches it is a
comment's-width blueprint (`src/jarvis/webui/api/visualizer.py`) and a state
reader with no reply-path logic of its own (`src/jarvis/webui/visualizer/state.py`).
That reader still imports Jarvis's own `jarvis.runtime.state`, and Jarvis's
TTS engines call into it to feed a live waveform — a real, if narrow, link
between AGPL code and the rest of the daemon.

The Kokoro engine is a materially closer link: `jarvis.output.vendor.kokoro_backtalk`
is imported directly by `jarvis.output.tts.KokoroTTS`, which is one of the
selectable engines behind every spoken reply the assistant gives, in the
same process and the same Python import graph as the rest of the daemon
whenever `tts_engine` is set to `"kokoro"`. Unlike the visualizer (reachable
as a separable view over the network), the Kokoro engine is linked into the
core reply/voice pipeline that every other module in this repository also
depends on. Whether that combined program can be conveyed under the
permissive Jarvis licence while one selectable, opt-in code path is AGPL, or
whether shipping that build requires the whole combination to carry AGPL
terms, is not resolved here. This is flagged for the maintainer to decide
rather than assumed by file layout; it does not by itself relicense any
other file in this repository, and Piper and Chatterbox remain unaffected
since they carry no AGPL code.
