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
    # How long a mid-turn phase may stand before it is treated as abandoned.
    # Handbacks are conditional, so a stage that loses the race to another
    # stage leaves its phase behind and nothing later clears it. Generous
    # enough that a genuinely long tool keeps its phase.
    phase_watchdog_sec: float = 180.0

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
    passive_enabled: bool = False
    passive_lines_written: int = 0
    passive_digests_produced: int = 0
    passive_last_line_at: Optional[float] = None
    conversation_active: bool = False

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

    def end_turn_phase(self) -> bool:
        """Return to idle from whatever mid-turn phase is still standing.

        The stage handbacks are conditional and can be skipped when another
        stage claimed the phase first, so the end of a turn is the one point
        that knows no stage of it is running any more. Dictation is not part
        of a turn and keeps the microphone regardless.
        """
        with self._lock:
            if self.phase in (Phase.DICTATING, Phase.IDLE, Phase.STARTING):
                return False
            self.phase = Phase.IDLE
            self.phase_since = time.time()
            snapshot = self._phase_snapshot()
        get_event_bus().publish("phase", snapshot)
        return True

    def _heal_stale_phase(self) -> Optional[dict]:
        """Drop a mid-turn phase nothing handed back. Caller holds the lock."""
        if self.phase in (Phase.DICTATING, Phase.IDLE, Phase.STARTING):
            return None
        if time.time() - self.phase_since < self.phase_watchdog_sec:
            return None
        self.phase = Phase.IDLE
        self.phase_since = time.time()
        return self._phase_snapshot()

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

    def set_passive_enabled(self, enabled: bool) -> None:
        """Publish a change to the live passive-capture switch."""
        enabled = bool(enabled)
        with self._lock:
            if self.passive_enabled == enabled:
                return
            self.passive_enabled = enabled
            snapshot = self._passive_snapshot()
        get_event_bus().publish("passive", snapshot)

    def record_passive_line(self) -> None:
        """Count a text line successfully written to the passive record."""
        with self._lock:
            self.passive_lines_written += 1
            self.passive_last_line_at = time.time()
            snapshot = self._passive_snapshot()
        get_event_bus().publish("passive", snapshot)

    def set_conversation_active(self, active: bool) -> None:
        """Publish a change to the wake-word-free conversation.

        The listener owns the conversation; this is the copy an interface
        can watch. A conversation also ends on its own, when the intent
        judge decides the user asked Jarvis to stop, so a page that only
        knew what it had itself switched on would go stale.
        """
        active = bool(active)
        with self._lock:
            if self.conversation_active == active:
                return
            self.conversation_active = active
            snapshot = self._conversation_snapshot()
        get_event_bus().publish("conversation", snapshot)

    def record_passive_digest(self) -> None:
        """Count a non-empty ambient digest successfully written to memory."""
        with self._lock:
            self.passive_digests_produced += 1
            snapshot = self._passive_snapshot()
        get_event_bus().publish("passive", snapshot)

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
            self.passive_enabled = False
            self.passive_lines_written = 0
            self.passive_digests_produced = 0
            self.passive_last_line_at = None
            self.conversation_active = False

    # ── reads ───────────────────────────────────────────────────────────

    def _phase_snapshot(self) -> dict:
        return {
            "phase": self.phase.value,
            "phase_since": self.phase_since,
            "phase_seconds": max(0.0, time.time() - self.phase_since),
        }

    def _passive_snapshot(self) -> dict:
        return {
            "enabled": self.passive_enabled,
            "lines_written": self.passive_lines_written,
            "digests_produced": self.passive_digests_produced,
            "last_line_at": self.passive_last_line_at,
        }

    def _conversation_snapshot(self) -> dict:
        return {"active": self.conversation_active}

    def snapshot(self) -> dict:
        """A JSON-ready view of everything the interface shows.

        Reading the state is also when an abandoned phase is noticed: the
        interface asking "what is Jarvis doing" is exactly the moment a phase
        left standing by a skipped handback becomes visible as a false hang.
        """
        with self._lock:
            healed = self._heal_stale_phase()
            view = {
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
                "passive": self._passive_snapshot(),
                "conversation": self._conversation_snapshot(),
            }
        if healed is not None:
            get_event_bus().publish("phase", healed)
        return view


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


def end_turn_phase() -> bool:
    """Shorthand for the unconditional return to idle at a turn's end."""
    return _state.end_turn_phase()
