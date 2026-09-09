"""Atomic runtime generations for live LLM route reconfiguration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .backend import LLMBackend
from .factory import get_llm_backend
from .route import RoutedBackend


_FINGERPRINT_SALT = os.urandom(32)


@dataclass(frozen=True)
class BackendGeneration:
    number: int
    settings: Any
    backend: LLMBackend
    activated_at: float
    fingerprint: str


def _settings_fingerprint(settings: Any) -> str:
    fields = {
        "routes": getattr(settings, "llm_routes", []) or [],
        "route_credentials": {
            route["api_key_env"]: os.environ.get(route["api_key_env"], "")
            for route in (getattr(settings, "llm_routes", []) or [])
            if isinstance(route, dict) and route.get("api_key_env")
        },
        "override": getattr(settings, "chat_backend_override", "auto") or "auto",
        "crew_agent": getattr(settings, "crew_chat_agent", "") or "",
        "crew_url": getattr(settings, "crew_api_url", "") or "",
        "crew_key": getattr(settings, "crew_api_key", "") or "",
        "ollama_url": getattr(settings, "ollama_base_url", "") or "",
        "ollama_model": getattr(settings, "ollama_chat_model", "") or "",
        "low_power": bool(getattr(settings, "low_power_mode", False)),
        "config_path": os.environ.get("JARVIS_CONFIG_PATH", ""),
        "state_path": os.environ.get("JARVIS_LLM_ROUTE_STATE_PATH", ""),
    }
    encoded = json.dumps(fields, sort_keys=True, default=str).encode("utf-8")
    # The fingerprint is exposed by the status API.  Salting it per process
    # keeps it useful for generation identity without turning it into an
    # offline oracle for short credentials stored in route configuration.
    return hashlib.sha256(_FINGERPRINT_SALT + encoded).hexdigest()


class LLMRuntime:
    """Own the active immutable settings/backend snapshot.

    A caller keeps the returned :class:`BackendGeneration` for its entire
    turn. Reconfiguration publishes a new object under the lock, so it cannot
    alter an in-flight turn.
    """

    def __init__(self, *, builder: Callable[[Any], LLMBackend] = get_llm_backend) -> None:
        self._builder = builder
        self._lock = threading.RLock()
        self._current: BackendGeneration | None = None
        self._retired: list[BackendGeneration] = []

    def _build_locked(self, settings: Any) -> LLMBackend:
        backend = self._builder(settings)
        if isinstance(backend, RoutedBackend):
            if self._current is not None and isinstance(
                self._current.backend, RoutedBackend
            ):
                crew_fields = ("crew_api_url", "crew_api_key", "crew_chat_agent")
                if all(getattr(settings, name, None) == getattr(self._current.settings, name, None)
                       for name in crew_fields):
                    backend.reuse_backends_from(self._current.backend)
            # Constructors are side-effect free, but building each configured
            # adapter here catches invalid generations before publication.
            try:
                for route in backend.routes:
                    if route.enabled:
                        backend._backend(route)
            except Exception:
                retained = set()
                if self._current is not None and isinstance(self._current.backend, RoutedBackend):
                    retained.update(id(adapter) for adapter in self._current.backend._backends.values())
                backend.close(retained)
                raise
        return backend

    def install(self, settings: Any) -> BackendGeneration:
        settings = copy.deepcopy(settings)
        fingerprint = _settings_fingerprint(settings)
        with self._lock:
            if self._current is not None and self._current.fingerprint == fingerprint:
                return self._current
            backend = self._build_locked(settings)
            return self._publish_locked(settings, backend, fingerprint)

    def _publish_locked(
        self, settings: Any, backend: LLMBackend, fingerprint: str,
    ) -> BackendGeneration:
        previous = self._current
        generation = BackendGeneration(
            number=(previous.number + 1 if previous is not None else 1),
            settings=settings,
            backend=backend,
            activated_at=time.time(),
            fingerprint=fingerprint,
        )
        self._current = generation
        if previous is not None:
            self._retired.append(previous)
        return generation

    def snapshot(self, settings: Any | None = None) -> BackendGeneration:
        if settings is not None:
            return self.install(settings)
        with self._lock:
            if self._current is None:
                raise RuntimeError("LLM runtime has not been initialised")
            return self._current

    def reconfigure(
        self,
        prepare: Callable[[], Any],
        rollback: Callable[[], None],
    ) -> BackendGeneration:
        """Persist/load/build/swap as one transaction from readers' view."""
        with self._lock:
            try:
                settings = copy.deepcopy(prepare())
                fingerprint = _settings_fingerprint(settings)
                backend = self._build_locked(settings)
            except Exception:
                rollback()
                raise
            return self._publish_locked(settings, backend, fingerprint)

    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._current
        if current is None:
            return {"generation": 0, "activated_at": None, "active_override": "auto"}
        backend = current.backend
        return {
            "generation": current.number,
            "activated_at": current.activated_at,
            "active_override": str(
                getattr(current.settings, "chat_backend_override", "auto") or "auto"
            ),
            "health": backend.health_summary() if isinstance(backend, RoutedBackend) else {},
        }

    def shutdown(self) -> None:
        with self._lock:
            generations = [*self._retired]
            if self._current is not None:
                generations.append(self._current)
            self._retired = []
            self._current = None
        seen: set[int] = set()
        for generation in generations:
            backend = generation.backend
            if id(backend) in seen:
                continue
            if isinstance(backend, RoutedBackend):
                backend.close(seen)
                continue
            seen.add(id(backend))
            close = getattr(backend, "close", None)
            if callable(close):
                close()


_RUNTIME = LLMRuntime()


def get_llm_runtime() -> LLMRuntime:
    return _RUNTIME


def reset_llm_runtime_for_tests() -> None:
    global _RUNTIME
    _RUNTIME.shutdown()
    _RUNTIME = LLMRuntime()
