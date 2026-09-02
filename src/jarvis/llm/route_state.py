"""Persistent health and cooldown state for LLM routes."""

from __future__ import annotations

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
    return Path.home() / ".jarvis" / "llm_routes_state.json"


def route_state_key(route: Any) -> str:
    """Return a stable, non-secret identifier for one configured route key."""
    raw = "\0".join((
        str(getattr(route, "tier", "")),
        str(getattr(route, "name", "")),
        str(getattr(route, "provider", "")),
        str(getattr(route, "api_key", "")),
        str(getattr(route, "api_key_env", "")),
    ))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{getattr(getattr(route, 'tier', None), 'value', 'chat')}:{digest}"


class RouteStateStore:
    """Thread-safe JSON store containing no URLs, model names, or credentials."""

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
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("routes"), dict):
                return raw
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return {"version": 1, "routes": {}}

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
        return self._data["routes"].setdefault(route_state_key(route), {
            "blocked_until": 0.0,
            "hits": 0,
            "failures": 0,
            "last_error": "",
            "rate_limits": 0,
        })

    def is_blocked(self, route: Any) -> bool:
        with self._lock:
            return float(self._entry(route).get("blocked_until", 0.0) or 0.0) > self._now()

    def is_invalid_for_run(self, route: Any) -> bool:
        with _RUN_INVALID_LOCK:
            return route_state_key(route) in _RUN_INVALID.get(str(self.path.resolve()), set())

    def mark_invalid_for_run(self, route: Any) -> None:
        with _RUN_INVALID_LOCK:
            _RUN_INVALID.setdefault(str(self.path.resolve()), set()).add(route_state_key(route))

    def record_hit(self, route: Any) -> None:
        with self._lock:
            entry = self._entry(route)
            entry["hits"] = int(entry.get("hits", 0)) + 1
            entry["last_error"] = ""
            self._persist()

    def record_failure(self, route: Any, error: BaseException | str) -> None:
        with self._lock:
            entry = self._entry(route)
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["last_error"] = error if isinstance(error, str) else type(error).__name__
            now = self._now()
            if isinstance(error, RateLimitedError):
                count = int(entry.get("rate_limits", 0)) + 1
                entry["rate_limits"] = count
                if error.retry_after is not None:
                    delay = max(0.0, float(error.retry_after))
                else:
                    delay = (60.0, 300.0, 900.0)[min(count - 1, 2)]
                entry["blocked_until"] = now + delay
            elif isinstance(error, QuotaExhaustedError):
                reset = error.reset_at
                if reset is None or float(reset) <= now:
                    current = datetime.fromtimestamp(now, tz=timezone.utc)
                    reset_dt = (current + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    reset = reset_dt.timestamp()
                entry["blocked_until"] = float(reset)
            self._persist()

    def status(self, route: Any) -> dict[str, Any]:
        with self._lock:
            entry = dict(self._entry(route))
        blocked_until = float(entry.get("blocked_until", 0.0) or 0.0)
        return {
            "blocked_until": (
                datetime.fromtimestamp(blocked_until, tz=timezone.utc).isoformat()
                if blocked_until > self._now() else None
            ),
            "hits": int(entry.get("hits", 0)),
            "failures": int(entry.get("failures", 0)),
            "last_error": str(entry.get("last_error", "") or ""),
        }

    def reset(self, route: Any | None = None) -> None:
        with self._lock:
            if route is None:
                self._data = {"version": 1, "routes": {}}
            else:
                self._data["routes"].pop(route_state_key(route), None)
            self._persist()
        with _RUN_INVALID_LOCK:
            invalid = _RUN_INVALID.setdefault(str(self.path.resolve()), set())
            if route is None:
                invalid.clear()
            else:
                invalid.discard(route_state_key(route))
