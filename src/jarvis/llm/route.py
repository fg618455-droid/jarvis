"""Tier-aware fallback routing across generic LLM protocol endpoints."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import requests

from ..debug import debug_log
from .backend import (
    AuthError,
    LLMBackend,
    ProviderError,
    ToolsNotSupportedError,
)
from .ollama import OllamaBackend
from .openai_compatible import OpenAICompatibleBackend
from .route_state import RouteStateStore
from .tiers import Tier


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


def _build_backend(route: Route) -> LLMBackend:
    if route.provider == "openai_compatible":
        return OpenAICompatibleBackend(route.base_url, api_key=route.api_key or None)
    return OllamaBackend(route.base_url)


class RoutedBackend(LLMBackend):
    """Walk ordered per-tier chains while preserving the fail-soft contract."""

    def __init__(
        self,
        routes: Iterable[Route],
        *,
        state_store: RouteStateStore | None = None,
        backend_factory: Callable[[Route], LLMBackend] = _build_backend,
    ) -> None:
        self._routes = tuple(routes)
        self._state = state_store or RouteStateStore()
        self._backend_factory = backend_factory
        self._backends: dict[Route, LLMBackend] = {}

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

    def _available(self, tier: Tier):
        for route in self.routes_for(tier):
            if self._state.is_invalid_for_run(route) or self._state.is_blocked(route):
                continue
            yield route

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

    def _run(self, model: str, invoke: Callable[[LLMBackend, Route], Any]):
        for route in self._available(self._tier(model)):
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
                  timeout_sec=30.0, thinking=False):
        emitted = False

        def forward(token: str) -> None:
            nonlocal emitted
            emitted = True
            if on_token:
                on_token(token)

        for route in self._available(self._tier(chat_model)):
            try:
                result = self._backend(route).streaming(
                    route.model, system_prompt, user_content, on_token=forward,
                    timeout_sec=self._timeout(timeout_sec, route), thinking=thinking,
                )
            except ToolsNotSupportedError:
                raise
            except (ProviderError, requests.exceptions.RequestException, TimeoutError) as error:
                self._failed(route, error)
                if emitted:
                    return None
                continue
            if result is None:
                self._failed(route, "EmptyResponse")
                if emitted:
                    return None
                continue
            self._state.record_hit(route)
            return result
        return None

    def chat(self, chat_model, messages, timeout_sec=30.0, extra_options=None,
             tools=None, thinking=False):
        return self._run(chat_model, lambda backend, route: backend.chat(
            route.model, messages, timeout_sec=self._timeout(timeout_sec, route),
            extra_options=extra_options, tools=tools, thinking=thinking,
        ))

    def embed(self, text, model, timeout_sec=15.0):
        return None

    def list_models(self, timeout_sec=5.0) -> list[str]:
        models: list[str] = []
        for route in self._routes:
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
        local = next((route for route in reversed(routes) if route.provider == "ollama"), None)
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
                    "active": route == active,
                    "invalid": self._state.is_invalid_for_run(route),
                    **status,
                })
            result[tier.value] = chain
        return result

    def reset(self, route: Route | None = None) -> None:
        self._state.reset(route)
