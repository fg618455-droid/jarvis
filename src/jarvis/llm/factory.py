"""Factories for resolving the active LLM and embedding backends.

Two factories share one provider catalogue:

- :func:`get_llm_backend` — chat / completion path. Dispatches on
  ``settings.llm_provider``.
- :func:`get_embedding_backend`: embeddings path. Route-chain configs
  always use loopback Ollama so stored vectors remain in one private vector
  space. Single-endpoint configs retain their explicit embedding provider.
"""

from __future__ import annotations
from typing import Any, Optional
from urllib.parse import urlparse
import weakref

from .backend import LLMBackend
from .ollama import OllamaBackend
from .openai_compatible import OpenAICompatibleBackend
from .route import Route, RoutedBackend
from .tiers import Tier


_OLLAMA = "ollama"
_OPENAI_COMPATIBLE = "openai_compatible"
_CLAUDE_SUBSCRIPTION = "claude_subscription"
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_ROUTER_CACHE: dict[int, tuple[weakref.ReferenceType[Any], RoutedBackend]] = {}

# Ceilings for the loopback Ollama routes. Ollama evicts a model once its
# keep-alive lapses, so the next call pays a page-in that runs into double
# digits of seconds for a 7B weight set. The local route is always last in
# its chain — there is nothing to fall forward to — so a ceiling shorter than
# a cold load would not buy speed, it would guarantee the route can never
# answer. The caller's own timeout still governs; these are only the maximum.
_LOCAL_FAST_TIMEOUT_SEC = 60.0
_LOCAL_CHAT_TIMEOUT_SEC = 180.0

# How long Ollama holds a model resident after a request. The long value is
# what makes a warm assistant stay warm; low power mode trades that away to
# give the GPU back between conversations.
OLLAMA_KEEP_ALIVE = "30m"
LOW_POWER_OLLAMA_KEEP_ALIVE = "1m"


def _resolve_provider(value: Any) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in (_OLLAMA, _OPENAI_COMPATIBLE):
            return v
    return _OLLAMA


def _resolve_route_provider(value: Any) -> str:
    """Resolve a provider name for one ``llm_routes`` chain entry.

    Unlike :func:`_resolve_provider` (used for the single-endpoint
    ``llm_provider``/``embedding_provider`` settings), this also accepts
    ``claude_subscription``: that route protocol is a CHAT-chain-only
    alternative, never a blanket single-endpoint choice, because the
    single-endpoint path also serves the FAST tier and embeddings, and a
    cloud subscription call has no place answering either — FAST needs a
    warm, low-latency local model, and embeddings must stay on loopback
    Ollama regardless of billing model.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in (_OLLAMA, _OPENAI_COMPATIBLE, _CLAUDE_SUBSCRIPTION):
            return v
    return _OLLAMA


def _str_attr(settings: Any, name: str, default: str = "") -> str:
    val = getattr(settings, name, None)
    return val if isinstance(val, str) and val else default


def is_low_power_mode(settings: Any) -> bool:
    """Whether the user asked Jarvis to give hardware back between turns."""
    return getattr(settings, "low_power_mode", False) is True


def ollama_keep_alive(settings: Any) -> str:
    """Return the Ollama residency duration for the active power mode."""
    if is_low_power_mode(settings):
        return LOW_POWER_OLLAMA_KEEP_ALIVE
    return OLLAMA_KEEP_ALIVE


def _build(provider: str, base_url: str, api_key: Optional[str]) -> LLMBackend:
    if provider == _OPENAI_COMPATIBLE:
        return OpenAICompatibleBackend(base_url, api_key=api_key)
    return OllamaBackend(base_url)


def get_llm_backend(settings: Any) -> LLMBackend:
    """Return the configured chat backend.

    ``llm_base_url`` is the OpenAI-compatible server's URL; the Ollama path
    uses ``ollama_base_url``. Keeping each provider on its own URL field
    means toggling ``llm_provider`` back to Ollama can never leave the
    backend pointed at a stale OpenAI-compatible URL.
    """
    cache_key = id(settings)
    cached = _ROUTER_CACHE.get(cache_key)
    if cached is not None and cached[0]() is settings:
        return cached[1]

    configured = getattr(settings, "llm_routes", None)
    routes: list[Route] = []
    if isinstance(configured, list):
        for index, raw in enumerate(configured):
            if not isinstance(raw, dict):
                continue
            try:
                tier = Tier(str(raw.get("tier", "")).strip().lower())
            except ValueError:
                continue
            if tier is Tier.PRIVATE:
                continue
            provider = _resolve_route_provider(raw.get("provider"))
            base_url = str(raw.get("base_url", "") or "").strip()
            model = str(raw.get("model", "") or "").strip()
            if not base_url or not model:
                continue
            try:
                timeout_sec = max(0.1, float(raw.get("timeout_sec", 4.0)))
            except (TypeError, ValueError):
                timeout_sec = 4.0
            raw_capabilities = raw.get(
                "capabilities", ["chat", "stream", "tools"]
            )
            capabilities = (
                frozenset(
                    capability
                    for capability in (
                        str(item).strip().lower() for item in raw_capabilities
                    )
                    if capability in {"chat", "stream", "tools"}
                )
                if isinstance(raw_capabilities, (list, tuple, set, frozenset))
                else frozenset({"chat", "stream", "tools"})
            )
            routes.append(Route(
                name=str(raw.get("name", "") or f"route-{index + 1}").strip(),
                provider=provider,
                base_url=base_url.rstrip("/"),
                api_key=str(raw.get("api_key", "") or ""),
                model=model,
                tier=tier,
                timeout_sec=timeout_sec,
                api_key_env=str(raw.get("api_key_env", "") or "").strip(),
                enabled=bool(raw.get("enabled", True)),
                capabilities=capabilities,
            ))

    configured_ollama_url = _str_attr(
        settings, "ollama_base_url", _DEFAULT_OLLAMA_URL
    ).rstrip("/")
    private_ollama_url = _loopback_ollama_url(settings)
    ollama_chat = _str_attr(settings, "ollama_chat_model") or _str_attr(
        settings, "llm_chat_model"
    )
    fast_model = _str_attr(settings, "fast_model") or ollama_chat
    # Every Ollama request renews the model's residency, so an idle stretch
    # between conversations never costs the next reply a cold page-in.
    keep_alive = ollama_keep_alive(settings)

    # A route the user switched off is inert. It stays in the chain so the
    # settings UI can still show it, but it does not get to decide whether the
    # local chain is a fallback or the only path there is.
    active_routes = [route for route in routes if route.enabled]

    if active_routes:
        local_defaults = {
            Tier.FAST: Route(
                "local-fast", _OLLAMA, private_ollama_url, "", fast_model,
                Tier.FAST, _LOCAL_FAST_TIMEOUT_SEC, keep_alive=keep_alive,
            ),
            Tier.CHAT: Route(
                "local-chat", _OLLAMA, private_ollama_url, "", ollama_chat,
                Tier.CHAT, _LOCAL_CHAT_TIMEOUT_SEC, keep_alive=keep_alive,
            ),
        }
        for tier in (Tier.FAST, Tier.CHAT):
            tier_routes = [route for route in active_routes if route.tier is tier]
            if not any(RoutedBackend._is_local(route) for route in tier_routes):
                routes.append(local_defaults[tier])
    else:
        provider = _resolve_provider(getattr(settings, "llm_provider", None))
        if provider == _OPENAI_COMPATIBLE:
            base_url = _str_attr(settings, "llm_base_url") or configured_ollama_url
            api_key = _str_attr(settings, "llm_api_key")
            chat_model = (
                _str_attr(settings, "llm_chat_model")
                or _str_attr(settings, "fast_model")
                or ollama_chat
            )
            routes.extend((
                Route("configured-fast", provider, base_url, api_key, fast_model,
                      Tier.FAST, _LOCAL_FAST_TIMEOUT_SEC),
                Route("configured-chat", provider, base_url, api_key, chat_model,
                      Tier.CHAT, _LOCAL_CHAT_TIMEOUT_SEC),
            ))
        else:
            routes.extend((
                Route("local-fast", _OLLAMA, configured_ollama_url, "", fast_model,
                      Tier.FAST, _LOCAL_FAST_TIMEOUT_SEC, keep_alive=keep_alive),
                Route("local-chat", _OLLAMA, configured_ollama_url, "", ollama_chat,
                      Tier.CHAT, _LOCAL_CHAT_TIMEOUT_SEC, keep_alive=keep_alive),
            ))
    routes.append(Route(
        "local-private", _OLLAMA, private_ollama_url, "", ollama_chat, Tier.PRIVATE,
        180.0, keep_alive=keep_alive,
    ))
    backend = RoutedBackend(routes)
    try:
        reference = weakref.ref(
            settings,
            lambda _reference, key=cache_key: _ROUTER_CACHE.pop(key, None),
        )
        _ROUTER_CACHE[cache_key] = (reference, backend)
    except TypeError:
        pass
    return backend


def _loopback_ollama_url(settings: Any) -> str:
    configured = _str_attr(settings, "ollama_base_url", _DEFAULT_OLLAMA_URL)
    try:
        host = (urlparse(configured).hostname or "").lower()
    except ValueError:
        host = ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        return configured.rstrip("/")
    return _DEFAULT_OLLAMA_URL


def get_embedding_backend(settings: Any) -> LLMBackend:
    """Return the configured embedding backend.

    Route-chain configs always return loopback Ollama. A single-endpoint
    config falls through ``embedding_provider`` to ``llm_provider`` and then
    Ollama, retaining the standalone backend contract.
    """
    if isinstance(getattr(settings, "llm_routes", None), list) and getattr(
        settings, "llm_routes", None
    ):
        return OllamaBackend(_loopback_ollama_url(settings))

    raw = getattr(settings, "embedding_provider", None)
    if isinstance(raw, str) and raw.strip():
        provider = _resolve_provider(raw)
    else:
        provider = _resolve_provider(getattr(settings, "llm_provider", None))

    base_url = _str_attr(settings, "embedding_base_url")
    if not base_url:
        if provider == _OPENAI_COMPATIBLE:
            base_url = _str_attr(settings, "llm_base_url")
        else:
            base_url = _str_attr(settings, "ollama_base_url", _DEFAULT_OLLAMA_URL)
    if not base_url:
        base_url = _DEFAULT_OLLAMA_URL
    api_key = _str_attr(settings, "embedding_api_key") or _str_attr(
        settings, "llm_api_key"
    ) or None
    return _build(provider, base_url, api_key)
