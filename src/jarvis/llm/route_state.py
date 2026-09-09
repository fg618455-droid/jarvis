"""Persistent, attempt-accurate health state for LLM routes."""

from __future__ import annotations

from jarvis.storage import state_directory

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .backend import QuotaExhaustedError, RateLimitedError

_RUN_INVALID: dict[str, set[str]] = {}
_RUN_INVALID_LOCK = threading.RLock()


def default_state_path() -> Path:
    override = os.environ.get("JARVIS_LLM_ROUTE_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return state_directory() / "llm_routes_state.json"


def route_state_key(route: Any) -> str:
    """Return a stable identifier tied to tier, provider, endpoint and model."""
    tier = getattr(getattr(route, "tier", None), "value", "chat")
    raw = "\0".join((
        str(tier),
        str(getattr(route, "provider", "") or "").strip().lower(),
        str(getattr(route, "base_url", "") or "").strip().rstrip("/"),
        str(getattr(route, "model", "") or "").strip(),
    ))
    return f"{tier}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _fresh_entry() -> dict[str, Any]:
    return {
        "attempts": 0,
        "successes": 0,
        "provider_failures": 0,
        "empty_responses": 0,
        "cooldown_skips": 0,
        "deadline_skips": 0,
        "stream_aborts": 0,
        "blocked_until": 0.0,
        "last_attempt_at": 0.0,
        "last_success_at": 0.0,
        "last_failure_at": 0.0,
        "last_error": "",
        "rate_limits": 0,
    }


class RouteStateStore:
    """Thread-safe JSON store containing no URL, model or credential text."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else default_state_path()
        self._now = now
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, UnicodeError):
            return {"version": 2, "routes": {}, "chains": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("routes"), dict):
            return {"version": 2, "routes": {}, "chains": {}}
        raw.setdefault("chains", {})
        raw["version"] = 2
        return raw

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def _persist(self) -> None:
        try:
            self._save()
        except OSError:
            pass

    def _entry(self, route: Any) -> dict[str, Any]:
        key = route_state_key(route)
        raw = self._data["routes"].setdefault(key, {})
        # Tolerant v1 read: old hits/failures remain visible as their closest
        # v2 meanings, while all new fields receive safe defaults.
        defaults = _fresh_entry()
        if "successes" not in raw and "hits" in raw:
            raw["successes"] = int(raw.get("hits", 0) or 0)
        if "provider_failures" not in raw and "failures" in raw:
            raw["provider_failures"] = int(raw.get("failures", 0) or 0)
        for name, value in defaults.items():
            raw.setdefault(name, value)
        return raw

    def is_blocked(self, route: Any) -> bool:
        with self._lock:
            return float(self._entry(route).get("blocked_until", 0.0) or 0.0) > self._now()

    def is_invalid_for_run(self, route: Any) -> bool:
        with _RUN_INVALID_LOCK:
            return route_state_key(route) in _RUN_INVALID.get(str(self.path.resolve()), set())

    def mark_invalid_for_run(self, route: Any) -> None:
        with _RUN_INVALID_LOCK:
            _RUN_INVALID.setdefault(str(self.path.resolve()), set()).add(route_state_key(route))

    def _stamp(self, entry: dict[str, Any], field: str) -> None:
        entry[field] = float(self._now())

    def record_attempt(self, route: Any) -> None:
        with self._lock:
            entry = self._entry(route)
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            self._stamp(entry, "last_attempt_at")
            self._persist()

    def record_success(self, route: Any) -> None:
        with self._lock:
            entry = self._entry(route)
            entry["successes"] = int(entry.get("successes", 0)) + 1
            entry["last_error"] = ""
            self._stamp(entry, "last_success_at")
            self._data["last_success_route"] = route_state_key(route)
            self._persist()

    def record_hit(self, route: Any) -> None:
        """Compatibility alias for callers predating attempt telemetry."""
        self.record_success(route)

    def _record_failure(
        self,
        route: Any,
        *,
        counter: str,
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            entry = self._entry(route)
            entry[counter] = int(entry.get(counter, 0)) + 1
            entry["last_error"] = reason
            self._stamp(entry, "last_failure_at")
            now = self._now()
            if isinstance(error, RateLimitedError):
                count = int(entry.get("rate_limits", 0)) + 1
                entry["rate_limits"] = count
                delay = (
                    max(0.0, float(error.retry_after))
                    if error.retry_after is not None
                    else (60.0, 300.0, 900.0)[min(count - 1, 2)]
                )
                entry["blocked_until"] = now + delay
            elif isinstance(error, QuotaExhaustedError):
                reset = error.reset_at
                if reset is None or float(reset) <= now:
                    current = datetime.fromtimestamp(now, tz=timezone.utc)
                    reset = (current + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ).timestamp()
                entry["blocked_until"] = float(reset)
            self._persist()

    def record_provider_failure(self, route: Any, error: BaseException) -> None:
        self._record_failure(
            route,
            counter="provider_failures",
            reason=type(error).__name__,
            error=error,
        )

    def record_empty_response(self, route: Any) -> None:
        self._record_failure(
            route, counter="empty_responses", reason="empty_response"
        )

    def record_stream_abort(self, route: Any, error: BaseException | None = None) -> None:
        self._record_failure(
            route,
            counter="stream_aborts",
            reason=type(error).__name__ if error is not None else "stream_aborted",
            error=error,
        )

    def record_failure(self, route: Any, error: BaseException | str) -> None:
        """Compatibility entry point with correct empty/provider semantics."""
        if isinstance(error, str) and "empty" in error.lower():
            self.record_empty_response(route)
        elif isinstance(error, BaseException):
            self.record_provider_failure(route, error)
        else:
            self._record_failure(
                route, counter="provider_failures", reason=str(error)
            )

    def record_cooldown_skip(self, route: Any) -> None:
        with self._lock:
            entry = self._entry(route)
            entry["cooldown_skips"] = int(entry.get("cooldown_skips", 0)) + 1
            self._persist()

    def record_deadline_skip(self, route: Any) -> None:
        with self._lock:
            entry = self._entry(route)
            entry["deadline_skips"] = int(entry.get("deadline_skips", 0)) + 1
            self._persist()

    def record_chain_exhaustion(self, tier: Any, reason: str = "exhausted") -> None:
        key = getattr(tier, "value", str(tier))
        with self._lock:
            entry = self._data.setdefault("chains", {}).setdefault(key, {
                "exhaustions": 0, "last_exhausted_at": 0.0, "last_reason": "",
            })
            entry["exhaustions"] = int(entry.get("exhaustions", 0)) + 1
            entry["last_exhausted_at"] = float(self._now())
            entry["last_reason"] = str(reason)
            self._persist()

    @staticmethod
    def _iso(timestamp: Any) -> str | None:
        value = float(timestamp or 0.0)
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None

    def status(self, route: Any) -> dict[str, Any]:
        with self._lock:
            entry = dict(self._entry(route))
            last_success = self._data.get("last_success_route")
        blocked_until = float(entry.get("blocked_until", 0.0) or 0.0)
        successes = int(entry.get("successes", 0))
        provider_failures = int(entry.get("provider_failures", 0))
        return {
            "attempts": int(entry.get("attempts", 0)),
            "successes": successes,
            "provider_failures": provider_failures,
            "empty_responses": int(entry.get("empty_responses", 0)),
            "cooldown_skips": int(entry.get("cooldown_skips", 0)),
            "deadline_skips": int(entry.get("deadline_skips", 0)),
            "stream_aborts": int(entry.get("stream_aborts", 0)),
            # Compatibility counters remain readable during the transition.
            "hits": successes,
            "failures": provider_failures + int(entry.get("empty_responses", 0)),
            "blocked_until": self._iso(blocked_until) if blocked_until > self._now() else None,
            "last_attempt_at": self._iso(entry.get("last_attempt_at")),
            "last_success_at": self._iso(entry.get("last_success_at")),
            "last_failure_at": self._iso(entry.get("last_failure_at")),
            "last_error": str(entry.get("last_error", "") or ""),
            "last_responded": last_success == route_state_key(route),
        }

    def chain_status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            chains = json.loads(json.dumps(self._data.get("chains", {})))
        for entry in chains.values():
            entry["last_exhausted_at"] = self._iso(entry.get("last_exhausted_at"))
        return chains

    def reset(self, route: Any | None = None) -> None:
        with self._lock:
            if route is None:
                self._data = {"version": 2, "routes": {}, "chains": {}}
            else:
                key = route_state_key(route)
                self._data["routes"].pop(key, None)
                if self._data.get("last_success_route") == key:
                    self._data.pop("last_success_route", None)
            self._persist()
        with _RUN_INVALID_LOCK:
            invalid = _RUN_INVALID.setdefault(str(self.path.resolve()), set())
            if route is None:
                invalid.clear()
            else:
                invalid.discard(route_state_key(route))
