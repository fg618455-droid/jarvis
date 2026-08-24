"""Tier-aware fallback routing across generic LLM protocol endpoints."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

from ..debug import debug_log
from .backend import (
    AuthError,
    LLMBackend,
    ProviderError,
    ToolsNotSupportedError,
)
from .claude_subscription import ClaudeSubscriptionBackend
from .crew_chat import CrewChatBackend
from .ollama import OllamaBackend
from .openai_compatible import OpenAICompatibleBackend
from .route_state import RouteStateStore
from .tiers import Tier


@dataclass(frozen=True)
class RequestDeadline:
    """A monotonic budget shared by the caller and route selection."""

    ends_at: float

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> "RequestDeadline":
        return cls(clock() + max(0.0, float(seconds)))

    def remaining(self, *, clock: Callable[[], float] = time.monotonic) -> float:
        return max(0.0, self.ends_at - clock())

    def expired(self, *, clock: Callable[[], float] = time.monotonic) -> bool:
        return self.remaining(clock=clock) <= 0.0


@dataclass(frozen=True)
class Route:
    """One model endpoint in a tier's ordered fallback chain."""

    name: str
    provider: str
    base_url: str
    api_key: str = field(repr=False, compare=True)
    model: str
    tier: Tier
    timeout_sec: float
    api_key_env: str = ""
    enabled: bool = True
    capabilities: frozenset[str] = frozenset({"chat", "stream", "tools"})
    # How long an Ollama runtime should hold this route's model resident after
    # each request. Empty leaves the server's own default in place, which is
    # what a remote OpenAI-compatible endpoint wants.
    keep_alive: str = ""


def _build_backend(route: Route, settings: Any = None) -> LLMBackend:
    """Build the concrete backend for one route.

    ``settings`` is only consulted for ``crew_chat``: that provider reuses
    ``cfg.crew_api_url`` / ``cfg.crew_api_key`` / ``cfg.crew_chat_agent``
    rather than the route's own ``base_url`` / ``api_key`` / ``model``
    fields, which stay inert placeholders (the same convention
    ``claude_subscription``'s ``base_url`` already uses). Every other
    provider ignores ``settings`` entirely, so a caller building a backend
    directly from a route (e.g. a test) never needs to supply it.
    """
    api_key = os.environ.get(route.api_key_env) if route.api_key_env else route.api_key
    if route.provider == "openai_compatible":
        return OpenAICompatibleBackend(route.base_url, api_key=api_key or None)
    if route.provider == "claude_subscription":
        return ClaudeSubscriptionBackend()
    if route.provider == "crew_chat":
        return CrewChatBackend(
            getattr(settings, "crew_api_url", "") or "",
            api_key=getattr(settings, "crew_api_key", "") or "",
            agent=getattr(settings, "crew_chat_agent", "") or "",
        )
    return OllamaBackend(route.base_url, keep_alive=route.keep_alive or None)


class RoutedBackend(LLMBackend):
    """Walk ordered per-tier chains while preserving the fail-soft contract."""

    def __init__(
        self,
        routes: Iterable[Route],
        *,
        state_store: RouteStateStore | None = None,
        backend_factory: Callable[[Route], LLMBackend] = _build_backend,
        clock: Callable[[], float] = time.monotonic,
        local_progress_sec: float = 1.2,
    ) -> None:
        self._routes = tuple(routes)
        self._state = state_store or RouteStateStore()
        self._backend_factory = backend_factory
        self._backends: dict[Route, LLMBackend] = {}
        self._clock = clock
        self.local_progress_sec = max(0.0, float(local_progress_sec))

    @property
    def routes(self) -> tuple[Route, ...]:
        return self._routes

    def routes_for(self, tier: Tier) -> tuple[Route, ...]:
        return tuple(route for route in self._routes if route.tier is tier)

    @staticmethod
    def _tier(model: str) -> Tier:
        tier = getattr(model, "tier", Tier.CHAT)
        return tier if isinstance(tier, Tier) else Tier.CHAT

    def _backend(self, route: Route) -> LLMBackend:
        backend = self._backends.get(route)
        if backend is None:
            backend = self._backend_factory(route)
            self._backends[route] = backend
        return backend

    def _available(self, tier: Tier, capability: str = "chat"):
        for route in self.routes_for(tier):
            if not route.enabled or capability not in route.capabilities:
                continue
            if self._state.is_invalid_for_run(route) or self._state.is_blocked(route):
                continue
            yield route

    @staticmethod
    def _ordered_for_preference(routes, preferred_provider: str | None):
        """Promote routes matching ``preferred_provider`` to the front of
        ``routes`` for one call, preserving relative order within each
        group. The rest of the chain stays reachable immediately after, so
        a promoted route that is unavailable or fails still falls through
        to the normal chain rather than ending the turn. No match for
        ``preferred_provider`` in ``routes`` (or no preference at all)
        returns the input order unchanged — the fail-open case a caller
        forcing an unconfigured provider depends on."""
        if not preferred_provider:
            return routes
        preferred = [route for route in routes if route.provider == preferred_provider]
        if not preferred:
            debug_log(
                f"LLM chat: preferred provider {preferred_provider!r} has no "
                "available route, falling through to the normal chain order",
                "llm",
            )
            return routes
        rest = [route for route in routes if route.provider != preferred_provider]
        return preferred + rest

    @staticmethod
    def _is_local(route: Route) -> bool:
        try:
            host = (urlparse(route.base_url).hostname or "").lower()
        except ValueError:
            return False
        return host in {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _timeout(caller_timeout: float, route: Route) -> float:
        return min(float(caller_timeout), float(route.timeout_sec))

    def _failed(self, route: Route, error: BaseException | str) -> None:
        if isinstance(error, AuthError):
            self._state.mark_invalid_for_run(route)
        self._state.record_failure(route, error)
        debug_log(
            f"LLM {route.tier.value} route failed ({error if isinstance(error, str) else type(error).__name__})",
            "llm",
        )

    def _run(
        self,
        model: str,
        invoke: Callable[[LLMBackend, Route], Any],
        *,
        capability: str = "chat",
        preferred_provider: str | None = None,
    ):
        candidates = self._ordered_for_preference(
            list(self._available(self._tier(model), capability)), preferred_provider,
        )
        for route in candidates:
            try:
                result = invoke(self._backend(route), route)
            except ToolsNotSupportedError:
                raise
            except (ProviderError, requests.exceptions.RequestException, TimeoutError) as error:
                self._failed(route, error)
                continue
            if result is None:
                self._failed(route, "EmptyResponse")
                continue
            self._state.record_hit(route)
            return result
        return None

    def direct(self, chat_model, system_prompt, user_content, timeout_sec=10.0,
               thinking=False, num_ctx=4096, temperature=None, max_tokens=None):
        return self._run(chat_model, lambda backend, route: backend.direct(
            route.model, system_prompt, user_content,
            timeout_sec=self._timeout(timeout_sec, route), thinking=thinking,
            num_ctx=num_ctx, temperature=temperature, max_tokens=max_tokens,
        ))

    def streaming(self, chat_model, system_prompt, user_content, on_token=None,
                  timeout_sec=30.0, thinking=False,
                  deadline: RequestDeadline | None = None):
        deadline = deadline or RequestDeadline.after(timeout_sec, clock=self._clock)
        available = list(self._available(self._tier(chat_model), "stream"))
        stream_routes = (
            [route for route in available if self._is_local(route)]
            + [route for route in available if not self._is_local(route)]
        )
        for route in stream_routes:
            remaining = deadline.remaining(clock=self._clock)
            if remaining <= 0.0:
                break

            lock = threading.RLock()
            progress_or_done = threading.Event()
            completed = threading.Event()
            state: dict[str, Any] = {
                "active": True,
                "emitted": False,
                "chunks": [],
                "result": None,
                "error": None,
            }

            def forward(token: str) -> None:
                if not token or not token.strip():
                    return
                with lock:
                    if not state["active"]:
                        return
                    state["emitted"] = True
                    state["chunks"].append(token)
                    progress_or_done.set()
                    if on_token:
                        on_token(token)

            def invoke() -> None:
                try:
                    state["result"] = self._backend(route).streaming(
                        route.model,
                        system_prompt,
                        user_content,
                        on_token=forward,
                        timeout_sec=min(
                            self._timeout(timeout_sec, route),
                            deadline.remaining(clock=self._clock),
                        ),
                        thinking=thinking,
                    )
                except Exception as error:
                    state["error"] = error
                finally:
                    completed.set()
                    progress_or_done.set()

            worker = threading.Thread(
                target=invoke,
                daemon=True,
                name=f"jarvis-route-{route.name}",
            )
            worker.start()
            progress_wait = (
                min(remaining, self.local_progress_sec)
                if self._is_local(route)
                else remaining
            )
            progress_or_done.wait(progress_wait)

            with lock:
                emitted = bool(state["emitted"])
                finished = completed.is_set()
                result = state["result"]
                error = state["error"]

            if emitted:
                completed.wait(deadline.remaining(clock=self._clock))
                with lock:
                    state["active"] = False
                    result = state["result"]
                    error = state["error"]
                    chunks = "".join(state["chunks"])
                if error is not None:
                    if isinstance(error, ToolsNotSupportedError):
                        raise error
                    if isinstance(error, (ProviderError, requests.exceptions.RequestException, TimeoutError)):
                        self._failed(route, error)
                    return None
                self._state.record_hit(route)
                return result or chunks

            with lock:
                state["active"] = False

            if finished and result is not None:
                if on_token and isinstance(result, str) and result.strip():
                    on_token(result)
                self._state.record_hit(route)
                return result
            if isinstance(error, ToolsNotSupportedError):
                raise error
            if isinstance(error, (ProviderError, requests.exceptions.RequestException, TimeoutError)):
                self._failed(route, error)
            elif error is not None:
                self._failed(route, ProviderError(type(error).__name__))
            else:
                self._failed(route, "ProgressDeadlineExceeded" if not finished else "EmptyResponse")
        return None

    def chat(self, chat_model, messages, timeout_sec=30.0, extra_options=None,
             tools=None, thinking=False, on_token=None, preferred_provider=None):
        # A route that cannot stream still answers, it just answers all at
        # once — so falling back through the chain never depends on whether
        # the caller wanted its text early.
        #
        # ``preferred_provider`` promotes routes of that provider to the
        # front of this one call's attempt order (see
        # ``_ordered_for_preference``); it never removes the rest of the
        # chain, so a promoted route that is missing or fails still falls
        # through exactly as it would with no preference at all.
        def invoke(backend, route):
            listener = on_token if "stream" in route.capabilities else None
            return backend.chat(
                route.model, messages, timeout_sec=self._timeout(timeout_sec, route),
                extra_options=extra_options, tools=tools, thinking=thinking,
                on_token=listener,
            )

        return self._run(
            chat_model, invoke,
            capability="tools" if tools else "chat",
            preferred_provider=preferred_provider,
        )

    def embed(self, text, model, timeout_sec=15.0):
        return None

    def list_models(self, timeout_sec=5.0) -> list[str]:
        models: list[str] = []
        routes = dict.fromkeys(
            route
            for tier in (Tier.FAST, Tier.CHAT)
            for route in self._available(tier, "chat")
        )
        for route in routes:
            try:
                found = self._backend(route).list_models(
                    timeout_sec=self._timeout(timeout_sec, route)
                )
            except (ProviderError, requests.exceptions.RequestException, TimeoutError) as error:
                self._failed(route, error)
                continue
            for model in found:
                if model not in models:
                    models.append(model)
        return models

    def warm_up(self, model, timeout_sec=60.0, keep_alive="30m") -> bool:
        routes = self.routes_for(self._tier(model))
        first_healthy = next(self._available(self._tier(model)), None)
        local = next((route for route in routes if self._is_local(route)), None)
        targets = []
        for route in (first_healthy, local):
            if route is not None and route not in targets:
                targets.append(route)
        warmed = False
        for route in targets:
            try:
                ok = self._backend(route).warm_up(
                    route.model,
                    timeout_sec=(
                        float(timeout_sec)
                        if route.provider == "ollama"
                        else self._timeout(timeout_sec, route)
                    ),
                    keep_alive=keep_alive,
                )
            except (ProviderError, requests.exceptions.RequestException, TimeoutError) as error:
                self._failed(route, error)
                continue
            if ok:
                warmed = True
                self._state.record_hit(route)
            else:
                self._failed(route, "WarmUpFailed")
        return warmed

    def route_status(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for tier in Tier:
            available = list(self._available(tier))
            active = available[0] if available else None
            chain = []
            for route in self.routes_for(tier):
                status = self._state.status(route)
                chain.append({
                    "id": f"{tier.value}:{route.name}",
                    "name": route.name,
                    "provider": route.provider,
                    "base_url": route.base_url,
                    "model": route.model,
                    "tier": tier.value,
                    "timeout_sec": route.timeout_sec,
                    "enabled": route.enabled,
                    "capabilities": sorted(route.capabilities),
                    "api_key_env": route.api_key_env,
                    "active": route == active,
                    "invalid": self._state.is_invalid_for_run(route),
                    **status,
                })
            result[tier.value] = chain
        return result

    def reset(self, route: Route | None = None) -> None:
        self._state.reset(route)
