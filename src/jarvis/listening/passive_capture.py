"""📝 Text-only passive transcript capture.

The voice listener already keeps a short rolling transcript. This module is
the process-wide switch and the safe eviction sink that can preserve those
final text segments. It never receives audio.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from ..debug import debug_log
from ..runtime import get_runtime_state
from .transcript_buffer import TranscriptSegment


class ClearableTranscriptBuffer(Protocol):
    def clear(self) -> None: ...


_enabled = False
_buffer: Optional[ClearableTranscriptBuffer] = None
_switch_listeners: list[Callable[[bool], None]] = []
_lock = threading.RLock()


def initialise_passive_capture(enabled: bool) -> None:
    """Initialise the running switch from the persisted setting."""
    global _enabled
    with _lock:
        _enabled = bool(enabled)
    get_runtime_state().set_passive_enabled(_enabled)
    debug_log(
        f"passive capture initialised {'on' if _enabled else 'off'}",
        "passive",
    )


def register_passive_buffer(buffer: Optional[ClearableTranscriptBuffer]) -> None:
    """Publish the listener's rolling buffer, or clear it on shutdown."""
    global _buffer
    with _lock:
        _buffer = buffer
    debug_log(
        f"passive capture buffer {'registered' if buffer else 'cleared'}",
        "passive",
    )


def register_passive_switch_listener(listener: Callable[[bool], None]) -> None:
    """Receive live switch changes, for example to manage a digest worker."""
    with _lock:
        if listener not in _switch_listeners:
            _switch_listeners.append(listener)


def unregister_passive_switch_listener(listener: Callable[[bool], None]) -> None:
    with _lock:
        try:
            _switch_listeners.remove(listener)
        except ValueError:
            pass


def passive_capture_enabled() -> bool:
    """Whether evicted transcript segments are being written."""
    with _lock:
        return _enabled


def set_passive_capture_enabled(enabled: bool) -> bool:
    """Flip passive capture immediately and drop buffered text when disabling."""
    global _enabled
    enabled = bool(enabled)
    with _lock:
        changed = _enabled != enabled
        _enabled = enabled
        buffer = _buffer
        listeners = list(_switch_listeners) if changed else []

    if not enabled and buffer is not None:
        try:
            buffer.clear()
        except Exception as exc:
            debug_log(f"passive buffer clear failed: {exc}", "passive")

    get_runtime_state().set_passive_enabled(enabled)
    if changed:
        debug_log(
            f"passive capture switched {'on' if enabled else 'off'}",
            "passive",
        )
    for listener in listeners:
        try:
            listener(enabled)
        except Exception as exc:
            debug_log(f"passive switch listener failed: {exc}", "passive")
    return changed


def clear_passive_buffer() -> None:
    """Drop live text so clearing the record cannot be undone by eviction."""
    with _lock:
        buffer = _buffer
    if buffer is None:
        return
    try:
        buffer.clear()
        debug_log("passive live buffer cleared with the record", "passive")
    except Exception as exc:
        debug_log(f"passive live buffer clear failed: {exc}", "passive")


def capture_evicted_segment(db, cfg, segment: TranscriptSegment) -> bool:
    """Write one final evicted segment, failing open on every error."""
    if not passive_capture_enabled():
        return False
    if segment.echo:
        debug_log("passive capture skipped an echo segment", "passive")
        return False

    min_words = max(0, int(getattr(cfg, "passive_capture_min_words", 3)))
    if len(segment.text.split()) < min_words:
        debug_log("passive capture skipped a short segment", "passive")
        return False

    try:
        started = datetime.fromtimestamp(segment.start_time, tz=timezone.utc)
        db.insert_passive_transcript(
            ts_utc=started.isoformat(),
            date_utc=started.date().isoformat(),
            duration_sec=max(0.0, float(segment.duration)),
            text=segment.text,
            language=segment.language,
            addressed=segment.processed,
            source_app="jarvis",
        )
    except Exception as exc:
        debug_log(f"passive transcript write failed: {exc}", "passive")
        get_runtime_state().record_error("passive transcript write failed")
        return False

    get_runtime_state().record_passive_line()
    debug_log("passive transcript line written", "passive")
    return True


def run_retention_sweep(db, cfg, *, now: Optional[datetime] = None) -> int:
    """Delete transcript dates older than the configured retention window."""
    try:
        retention_days = max(
            0,
            int(getattr(cfg, "passive_capture_retention_days", 30)),
        )
        if retention_days == 0:
            debug_log("passive retention keeps lines until manual deletion", "passive")
            return 0
        current = now or datetime.now(timezone.utc)
        cutoff = (current - timedelta(days=retention_days)).date().isoformat()
        deleted = db.delete_passive_transcripts_before(cutoff)
        debug_log(
            f"passive retention sweep deleted {deleted} lines before {cutoff}",
            "passive",
        )
        return deleted
    except Exception as exc:
        debug_log(f"passive retention sweep failed: {exc}", "passive")
        get_runtime_state().record_error("passive retention sweep failed")
        return 0


class PassiveRetentionWorker:
    """Run retention off the startup and audio threads, then once per day."""

    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="jarvis-passive-retention",
            daemon=True,
        )
        self._thread.start()
        debug_log("passive retention worker started", "passive")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        debug_log("passive retention worker stopped", "passive")

    def _run(self) -> None:
        run_retention_sweep(self.db, self.cfg)
        while not self._stop.wait(24 * 60 * 60):
            run_retention_sweep(self.db, self.cfg)
