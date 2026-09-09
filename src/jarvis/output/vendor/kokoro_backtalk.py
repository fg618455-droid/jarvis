# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kokoro streaming text-to-speech, vendored from backtalk's ``mouth.py``.

Only the Kokoro half of backtalk's mouth is here: the ElevenLabs cloud path
and its keychain lookup are not part of this file, by design (Jarvis's voice
pipeline is offline-first with no exceptions). The functions below take their
voice and speed as arguments instead of reading backtalk's global ``CFG`` and
call ``jarvis.debug.debug_log`` instead of backtalk's own logger, so
:class:`jarvis.output.tts.KokoroTTS` can call them directly. The synthesis
algorithm, the sentence splitting, and the espeak-ng discovery are otherwise
exactly backtalk's own code.

Default engine: Kokoro, in-process. Local, free, no server, no API key,
~0.2s to first audio once warm.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Iterator, List, Optional

import numpy as np

from ...debug import debug_log

KOKORO_RATE = 24000
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_pipe = None
_pipe_lang: Optional[str] = None
_pipe_lock = threading.Lock()


def ensure_espeak() -> None:
    """kokoro phonemizes through system espeak-ng (its bundled loader
    ships a broken build path — found the hard way; upstream's own docs
    say install the system package). Help phonemizer find it in the
    usual homes when the env isn't already set."""
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return
    candidates = (
        "/opt/homebrew/lib/libespeak-ng.dylib",       # macOS arm64 (brew)
        "/usr/local/lib/libespeak-ng.dylib",          # macOS intel (brew)
        "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1",  # debian/ubuntu
        "/usr/lib/libespeak-ng.so.1",                 # other linux
        "C:\\Program Files\\eSpeak NG\\libespeak-ng.dll",       # windows
        "C:\\Program Files (x86)\\eSpeak NG\\libespeak-ng.dll",
    )
    for lib in candidates:
        if os.path.exists(lib):
            os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = lib
            break


def warm(voice: str):
    """Load the Kokoro pipeline for ``voice``'s language (first call
    downloads the model to the HF cache). Reloaded only when the voice's
    language changes, since the pipeline is keyed by language, not voice."""
    global _pipe, _pipe_lang
    # The voice name's first letter IS the language pipeline:
    # a=American English, b=British English, e/f/h/i/j/p/z = the
    # other shipped languages. bm_lewis -> 'b'.
    lang = (voice or "bm_lewis")[0]
    with _pipe_lock:
        if _pipe is None or _pipe_lang != lang:
            ensure_espeak()
            from kokoro import KPipeline
            debug_log(f"loading kokoro (lang '{lang}', voice {voice})", "tts")
            _pipe = KPipeline(lang_code=lang)
            _pipe_lang = lang
            debug_log("kokoro voice ready", "tts")
    return _pipe


def split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def stream_kokoro(text: str, voice: str, speed: float = 1.0) -> Iterator["np.ndarray"]:
    """One utterance -> int16 PCM chunks at 24kHz, in-process."""
    pipe = warm(voice)
    for _, _, audio in pipe(text, voice=voice, speed=speed):
        a = np.asarray(audio, dtype=np.float32)
        if a.size:
            yield (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)
