"""🔵 Live runtime state.

What the assistant is doing right now, and the tallies that make a session
readable at a glance. The voice path writes to it as it moves between
stages; the control centre reads it.

Every write is cheap: a lock, a few assignments, and one event published to
whoever is watching. Nothing here reaches for the network, the disk, or a
model.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .events import get_event_bus


class Phase(str, Enum):
    """What the assistant is doing, in the order a turn passes through."""

    STARTING = "starting"          # models still loading
    IDLE = "idle"                  # waiting for the wake word
    CAPTURING = "capturing"        # voice detected, recording the utterance
    TRANSCRIBING = "transcribing"  # Whisper running
    THINKING = "thinking"          # reply engine running
    TOOL = "tool"                  # a tool is executing
    SPEAKING = "speaking"          # synthesised speech is playing
    DICTATING = "dictating"        # hold-to-dictate has the microphone


@dataclass
class RuntimeState:
    """The assistant's current phase and this session's tallies."""

    phase: Phase = Phase.STARTING
    phase_since: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)

    turns_voice: int = 0
    turns_text: int = 0
    tool_calls: int = 0
    errors: int = 0
    # Why an utterance never became a turn, keyed by reason. Silent discards
    # are the usual cause of "it ignored me", so they are counted rather
    # than only logged.
    discarded: dict[str, int] = field(default_factory=dict)

    last_error: Optional[str] = None
    last_error_at: Optional[float] = None
    last_turn: Optional[dict] = None

    models: dict[str, Any] = field(default_factory=dict)
    audio: dict[str, Any] = field(default_factory=dict)

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── writes ──────────────────────────────────────────────────────────

    def set_phase(self, phase: Phase) -> None:
        """Move to a phase, announcing it only when it actually changed."""
        with self._lock:
            if self.phase is phase:
                return
            self.phase = phase
            self.phase_since = time.time()
            snapshot = self._phase_snapshot()
        get_event_bus().publish("phase", snapshot)

    def set_phase_if(self, expected: Phase, phase: Phase) -> bool:
        """Move to a phase only if the assistant is still in ``expected``.

        Lets a stage hand the phase back when nothing else took over, without
        overwriting a phase a later stage has already claimed.
        """
        with self._lock:
            if self.phase is not expected or self.phase is phase:
                return False
            self.phase = phase
            self.phase_since = time.time()
            snapshot = self._phase_snapshot()
        get_event_bus().publish("phase", snapshot)
        return True

    def count_turn(self, source: str) -> None:
        with self._lock:
            if source == "text":
                self.turns_text += 1
            else:
                self.turns_voice += 1

    def count_tool_call(self) -> None:
        with self._lock:
            self.tool_calls += 1

    def count_discard(self, reason: str) -> None:
        with self._lock:
            self.discarded[reason] = self.discarded.get(reason, 0) + 1
            snapshot = dict(self.discarded)
        get_event_bus().publish("discarded", {"reason": reason, "totals": snapshot})

    def record_error(self, message: str) -> None:
        with self._lock:
            self.errors += 1
            self.last_error = message
            self.last_error_at = time.time()
        get_event_bus().publish("error", {"message": message})

    def record_turn(self, turn: dict) -> None:
        with self._lock:
            self.last_turn = turn
        get_event_bus().publish("turn", turn)

    def describe_models(self, **models: Any) -> None:
        with self._lock:
            self.models.update({k: v for k, v in models.items() if v is not None})

    def describe_audio(self, **audio: Any) -> None:
        with self._lock:
            self.audio.update({k: v for k, v in audio.items() if v is not None})

    def reset(self) -> None:
        """Return to a fresh session. Used when the daemon restarts in-process."""
        with self._lock:
            self.phase = Phase.STARTING
            self.phase_since = time.time()
            self.started_at = time.time()
            self.turns_voice = 0
            self.turns_text = 0
            self.tool_calls = 0
            self.errors = 0
            self.discarded = {}
            self.last_error = None
            self.last_error_at = None
            self.last_turn = None
            self.models = {}
            self.audio = {}

    # ── reads ───────────────────────────────────────────────────────────

    def _phase_snapshot(self) -> dict:
        return {
            "phase": self.phase.value,
            "phase_since": self.phase_since,
            "phase_seconds": max(0.0, time.time() - self.phase_since),
        }

    def snapshot(self) -> dict:
        """A JSON-ready view of everything the interface shows."""
        with self._lock:
            return {
                **self._phase_snapshot(),
                "started_at": self.started_at,
                "uptime_seconds": max(0.0, time.time() - self.started_at),
                "turns": {
                    "voice": self.turns_voice,
                    "text": self.turns_text,
                    "total": self.turns_voice + self.turns_text,
                },
                "tool_calls": self.tool_calls,
                "errors": self.errors,
                "discarded": dict(self.discarded),
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_turn": self.last_turn,
                "models": dict(self.models),
                "audio": dict(self.audio),
            }


_state = RuntimeState()


def get_runtime_state() -> RuntimeState:
    """The process-wide state. One daemon, one state."""
    return _state


def set_phase(phase: Phase) -> None:
    """Shorthand for the call sites scattered through the voice path."""
    _state.set_phase(phase)


def set_phase_if(expected: Phase, phase: Phase) -> bool:
    """Shorthand for a conditional hand-back from a stage."""
    return _state.set_phase_if(expected, phase)
