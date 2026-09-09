"""⏱️ Per-turn stage timings.

Measures where the time between "you stopped speaking" and "you hear the
first word back" actually goes: endpointing, transcription, recall,
planning, each tool, the model's first token, and speech synthesis.

The measurement runs in the reply path, so it is built to cost nothing
worth measuring: a ``perf_counter()`` reading and a list append per stage,
no logging, no formatting, no I/O until the turn is over.

A turn is tracked per thread, so a typed turn from the control centre and a
spoken turn can be in flight at the same time without mixing their stages.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from jarvis.debug import debug_log

from .events import get_event_bus
from .state import get_runtime_state


HISTORY_SIZE = 50
JOURNAL_MAX_BYTES = 5 * 1024 * 1024


def read_turn_journal(path: Path, limit: Optional[int] = None) -> list[dict]:
    """Read persisted turns oldest-first, tolerating a torn final line.

    Rotation renames the older file to ``turns.jsonl.1`` before a new current
    journal is opened, so that file is read first. A corrupt line is skipped
    independently: a power loss while appending one record must not hide the
    valid history surrounding it.
    """
    target = Path(path)
    records: list[dict] = []
    for candidate in (target.with_suffix(target.suffix + ".1"), target):
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            continue
    return records[-limit:] if limit else records


@dataclass
class Stage:
    """One measured step of a turn."""
    name: str
    start_ms: float
    duration_ms: float


@dataclass
class ToolCall:
    """One tool the reply loop ran."""
    name: str
    duration_ms: float
    ok: bool
    error: Optional[str] = None
    confirmed: Optional[bool] = None


@dataclass
class TurnTrace:
    """Everything measured about a single exchange."""

    turn_id: str
    source: str                       # "voice" | "text"
    started_at: float                 # wall clock, the moment speaking ended
    transcript: str = ""
    language: Optional[str] = None
    language_probability: Optional[float] = None
    stages: list[Stage] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    reply: Optional[str] = None
    total_ms: Optional[float] = None
    error: Optional[str] = None

    _origin: float = field(default_factory=time.perf_counter, repr=False)

    # ── measuring ───────────────────────────────────────────────────────

    def _now_ms(self) -> float:
        return (time.perf_counter() - self._origin) * 1000.0

    def mark(self, name: str, duration_ms: float, start_ms: Optional[float] = None) -> None:
        """Record a stage whose duration was measured elsewhere."""
        at = self._now_ms() - duration_ms if start_ms is None else start_ms
        self.stages.append(Stage(name=name, start_ms=max(0.0, at), duration_ms=duration_ms))

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Measure the block it wraps, including one that raises."""
        start = self._now_ms()
        begun = time.perf_counter()
        try:
            yield
        finally:
            self.stages.append(Stage(
                name=name,
                start_ms=start,
                duration_ms=(time.perf_counter() - begun) * 1000.0,
            ))

    def record_tool(self, name: str, duration_ms: float, ok: bool,
                    error: Optional[str] = None,
                    confirmed: Optional[bool] = None) -> None:
        self.tools.append(ToolCall(
            name=name, duration_ms=duration_ms, ok=ok, error=error, confirmed=confirmed,
        ))

    # ── reading ─────────────────────────────────────────────────────────

    def elapsed_ms(self) -> float:
        return self._now_ms()

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_origin", None)
        return data


class TurnRecorder:
    """Holds the turn in flight per thread, plus the recent history."""

    def __init__(self, history_size: int = HISTORY_SIZE) -> None:
        self._local = threading.local()
        self._history: deque[dict] = deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._journal_path: Optional[Path] = None

    # ── lifecycle ───────────────────────────────────────────────────────

    def begin(self, source: str = "voice", started_at: Optional[float] = None) -> TurnTrace:
        trace = TurnTrace(
            turn_id=uuid.uuid4().hex[:12],
            source=source,
            started_at=started_at if started_at is not None else time.time(),
        )
        self._local.trace = trace
        return trace

    def current(self) -> Optional[TurnTrace]:
        """The turn this thread is in, or ``None`` outside a turn."""
        return getattr(self._local, "trace", None)

    def finish(self, reply: Optional[str] = None, error: Optional[str] = None,
               trace: Optional[TurnTrace] = None) -> Optional[dict]:
        """Close a turn and file it.

        A turn can end on a different thread from the one that opened it:
        speech synthesis runs on its own worker, and that is where the wait
        the user feels actually ends. Such a caller passes the trace it
        holds instead of relying on the thread it happens to be on.
        """
        if trace is None:
            trace = self.current()
        if trace is None or trace.total_ms is not None:
            return None
        if self.current() is trace:
            self._local.trace = None

        trace.total_ms = trace.elapsed_ms()
        if reply is not None:
            trace.reply = reply
        if error is not None:
            trace.error = error

        record = trace.to_dict()
        with self._lock:
            self._history.append(record)

        state = get_runtime_state()
        state.count_turn(trace.source)
        state.record_turn(record)
        self._append_to_journal(record)
        return record

    def abandon(self) -> None:
        """Drop the turn in flight without recording it."""
        self._local.trace = None

    # ── history ─────────────────────────────────────────────────────────

    def history(self, limit: Optional[int] = None) -> list[dict]:
        with self._lock:
            records = list(self._history)
        return records[-limit:] if limit else records

    def clear(self) -> None:
        with self._lock:
            self._history.clear()

    # ── journal ─────────────────────────────────────────────────────────

    def use_journal(self, path: Optional[Path]) -> None:
        """Persist finished turns to a file, or stop persisting when ``None``."""
        self._journal_path = Path(path) if path else None

    def _append_to_journal(self, record: dict) -> None:
        path = self._journal_path
        if path is None:
            return
        try:
            if path.exists() and path.stat().st_size >= JOURNAL_MAX_BYTES:
                rotated = path.with_suffix(path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                os.replace(path, rotated)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            # A full disk must not take a reply down with it.
            debug_log(f"turn journal write failed: {exc}", "runtime")


_recorder = TurnRecorder()


def get_recorder() -> TurnRecorder:
    """The process-wide recorder. One daemon, one history."""
    return _recorder


def current_turn() -> Optional[TurnTrace]:
    """The turn this thread is in, for call sites that may run outside one."""
    return _recorder.current()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Measure a block when a turn is in flight, do nothing when it is not.

    Lets deep call sites be instrumented without every caller having to
    know whether it was reached from a turn.
    """
    trace = _recorder.current()
    if trace is None:
        yield
        return
    with trace.stage(name):
        yield


def mark(name: str, duration_ms: float) -> None:
    """Record a stage measured by the caller, if a turn is in flight.

    For blocks too long to wrap in a context manager without reshaping the
    code around them.
    """
    trace = _recorder.current()
    if trace is not None:
        trace.mark(name, duration_ms)


def record_tool(name: str, duration_ms: float, ok: bool,
                error: Optional[str] = None,
                confirmed: Optional[bool] = None) -> None:
    """Attach a tool call to the turn in flight, if there is one."""
    get_runtime_state().count_tool_call()
    trace = _recorder.current()
    if trace is not None:
        trace.record_tool(name, duration_ms, ok, error=error, confirmed=confirmed)


def publish_progress(name: str) -> None:
    """Tell watchers a turn reached a stage, without waiting for it to end."""
    trace = _recorder.current()
    if trace is None:
        return
    get_event_bus().publish("stage", {
        "turn_id": trace.turn_id,
        "stage": name,
        "elapsed_ms": trace.elapsed_ms(),
    })
