"""Lookup for the subscription provider adapters."""

from __future__ import annotations

from typing import Callable, Mapping

from .base import ProviderAdapter, ProviderId
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .hermes import HermesAdapter


_ADAPTERS: Mapping[ProviderId, Callable[[], ProviderAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "hermes": HermesAdapter,
}


def list_providers() -> tuple[ProviderId, ...]:
    """Return every provider identifier the registry can build."""

    return tuple(_ADAPTERS)


def get_provider(provider: str) -> ProviderAdapter:
    """Build the adapter for ``provider``, or reject an unknown name."""

    try:
        factory = _ADAPTERS[provider]  # type: ignore[index]
    except KeyError as error:
        known = ", ".join(_ADAPTERS)
        raise ValueError(f"Unsupported provider: {provider}. Known providers: {known}") from error
    return factory()
