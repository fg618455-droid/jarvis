"""🎭 Visualizer live state.

Bridges Jarvis's own runtime phase and TTS playback into the shape the
vendored ai-visualizer face expects: ``idle``, ``listening``, ``thinking``,
``speaking``, plus a waveform for a face to move to while speaking. This is
the direct in-process replacement for the three ``.voice_*`` signal files the
vendored code was originally written to poll from a second process — no
files, no second server, one process reading its own live objects.

Jarvis has no equivalent of ai-visualizer's own attention signal or of a
locally played thinking sound (Jarvis's TTS engines only ever play the
reply itself), so ``alert`` and ``loading`` are always reported as off
rather than invented.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable, List, Tuple

from ...debug import debug_log
from ...runtime.state import Phase, get_runtime_state

# A waveform older than this belongs to a block of audio that has already
# finished playing, so it is dropped rather than shown as though speech were
# still happening. Mirrors ai-visualizer's own server.py staleness window.
WAVEFORM_STALE_SECONDS = 0.6
SAMPLE_COUNT = 64

_PHASE_TO_VISUAL_STATE = {
    Phase.STARTING: "idle",
    Phase.IDLE: "idle",
    Phase.CAPTURING: "listening",
    Phase.TRANSCRIBING: "thinking",
    Phase.THINKING: "thinking",
    Phase.TOOL: "thinking",
    Phase.SPEAKING: "speaking",
    Phase.DICTATING: "idle",
}


class VisualizerWaveform:
    """The most recent block of audio a TTS engine wrote to the speakers.

    A TTS engine calls :meth:`feed` with each block it plays, the same
    moment backtalk's ``mouth.py`` calls ``signals.feed_waveform``. The
    difference is that this holds the samples in memory for the next poll
    of ``/api/visualizer/state`` instead of writing them to a file for a
    second process to read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: List[float] = []
        self._ts: float = 0.0

    def feed(self, samples: Iterable[float]) -> None:
        """Record the newest played block, most recent ``SAMPLE_COUNT`` first."""
        try:
            values = [float(s) for s in samples]
        except (TypeError, ValueError):
            return
        if not values:
            return
        with self._lock:
            self._samples = values[-SAMPLE_COUNT:]
            self._ts = time.time()

    def read(self) -> Tuple[List[float], bool]:
        """The samples and whether they are fresh enough to trust."""
        with self._lock:
            samples, ts = list(self._samples), self._ts
        fresh = bool(samples) and (time.time() - ts) < WAVEFORM_STALE_SECONDS
        return samples, fresh

    def reset(self) -> None:
        with self._lock:
            self._samples = []
            self._ts = 0.0


_waveform = VisualizerWaveform()
_last_reported_state = None
_state_lock = threading.Lock()


def get_visualizer_waveform() -> VisualizerWaveform:
    """The process-wide waveform holder. One daemon, one holder."""
    return _waveform


def _phase_to_visual_state(phase_value: str) -> str:
    try:
        return _PHASE_TO_VISUAL_STATE[Phase(phase_value)]
    except ValueError:
        return "idle"


def visualizer_state() -> dict:
    """The face's poll response, derived entirely from Jarvis's own state."""
    global _last_reported_state

    phase_value = get_runtime_state().snapshot()["phase"]
    state = _phase_to_visual_state(phase_value)

    samples, fresh = _waveform.read()
    level = 0.0
    out_samples = [0.0] * SAMPLE_COUNT
    if fresh:
        # A fresh waveform IS speech, the same rule ai-visualizer's own
        # server.py applies: trust what is actually leaving the speakers
        # over a phase reading that has not caught up yet.
        state = "speaking"
        padded = samples[-SAMPLE_COUNT:]
        out_samples = [0.0] * (SAMPLE_COUNT - len(padded)) + padded
        level = min(1.0, (sum(abs(s) for s in padded) / len(padded)) / 3000.0)

    with _state_lock:
        if state != _last_reported_state:
            debug_log(f"visualizer state -> {state}", "webui")
            _last_reported_state = state

    return {
        "state": state,
        "level": level,
        "samples": out_samples,
        "alert": False,
        "loading": False,
    }
