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
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_ROUTER_CACHE: dict[int, tuple[weakref.ReferenceType[Any], RoutedBackend]] = {}


def _resolve_provider(value: Any) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in (_OLLAMA, _OPENAI_COMPATIBLE):
            return v
    return _OLLAMA


def _str_attr(settings: Any, name: str, default: str = "") -> str:
    val = getattr(settings, name, None)
    return val if isinstance(val, str) and val else default


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
            provider = _resolve_provider(raw.get("provider"))
            base_url = str(raw.get("base_url", "") or "").strip()
            model = str(raw.get("model", "") or "").strip()
            if not base_url or not model:
                continue
            try:
                timeout_sec = max(0.1, float(raw.get("timeout_sec", 4.0)))
            except (TypeError, ValueError):
                timeout_sec = 4.0
            routes.append(Route(
                name=str(raw.get("name", "") or f"route-{index + 1}").strip(),
                provider=provider,
                base_url=base_url.rstrip("/"),
                api_key=str(raw.get("api_key", "") or ""),
                model=model,
                tier=tier,
                timeout_sec=timeout_sec,
            ))

    configured_ollama_url = _str_attr(
        settings, "ollama_base_url", _DEFAULT_OLLAMA_URL
    ).rstrip("/")
    private_ollama_url = _loopback_ollama_url(settings)
    ollama_chat = _str_attr(settings, "ollama_chat_model") or _str_attr(
        settings, "llm_chat_model"
    )
    fast_model = (
        ollama_chat if routes else _str_attr(settings, "fast_model") or ollama_chat
    )

    if routes:
        routes.extend((
            Route("local-fast", _OLLAMA, private_ollama_url, "", fast_model, Tier.FAST, 4.0),
            Route("local-chat", _OLLAMA, private_ollama_url, "", ollama_chat, Tier.CHAT, 4.0),
        ))
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
                Route("configured-fast", provider, base_url, api_key, fast_model, Tier.FAST, 60.0),
                Route("configured-chat", provider, base_url, api_key, chat_model, Tier.CHAT, 180.0),
            ))
        else:
            routes.extend((
                Route("local-fast", _OLLAMA, configured_ollama_url, "", fast_model, Tier.FAST, 60.0),
                Route("local-chat", _OLLAMA, configured_ollama_url, "", ollama_chat, Tier.CHAT, 180.0),
            ))
    routes.append(Route(
        "local-private", _OLLAMA, private_ollama_url, "", ollama_chat, Tier.PRIVATE, 180.0
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
