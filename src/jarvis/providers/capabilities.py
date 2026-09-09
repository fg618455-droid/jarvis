"""Evidence registry for provider and model capabilities."""

from __future__ import annotations

from threading import RLock

from jarvis.debug import debug_log

from .base import Capabilities, ProviderId


_PROVIDER_IDS = frozenset({"claude", "codex", "hermes"})


def _validate_provider(provider: str) -> None:
    if provider not in _PROVIDER_IDS:
        raise ValueError(f"Unsupported provider: {provider}")


def _validate_model(model: str | None) -> None:
    if model is not None and not model.strip():
        raise ValueError("A model identifier cannot be blank")


class ProviderCapabilityRegistry:
    """Store only capabilities established by provider-backed evidence."""

    def __init__(self) -> None:
        self._provider_capabilities: dict[ProviderId, Capabilities] = {}
        self._model_capabilities: dict[tuple[ProviderId, str], Capabilities] = {}
        self._lock = RLock()

    def register(
        self,
        provider: ProviderId,
        capabilities: Capabilities,
        *,
        model: str | None = None,
    ) -> None:
        """Record a complete provider-level or model-level capability result."""

        _validate_provider(provider)
        _validate_model(model)
        with self._lock:
            if model is None:
                self._provider_capabilities[provider] = capabilities
                scope = "provider"
            else:
                self._model_capabilities[(provider, model)] = capabilities
                scope = "model"
            debug_log(f"Recorded {scope} capabilities for {provider}", "providers")

    def get(self, provider: ProviderId, model: str | None = None) -> Capabilities:
        """Return the strongest applicable evidence, or fail closed."""

        _validate_provider(provider)
        _validate_model(model)
        with self._lock:
            if model is not None:
                model_capabilities = self._model_capabilities.get((provider, model))
                if model_capabilities is not None:
                    return model_capabilities
            return self._provider_capabilities.get(provider, Capabilities())

    def clear(self, provider: ProviderId | None = None) -> None:
        """Remove one provider's evidence or reset the registry."""

        if provider is not None:
            _validate_provider(provider)
        with self._lock:
            if provider is None:
                self._provider_capabilities.clear()
                self._model_capabilities.clear()
                debug_log("Cleared all provider capability evidence", "providers")
                return
            self._provider_capabilities.pop(provider, None)
            self._model_capabilities = {
                key: value
                for key, value in self._model_capabilities.items()
                if key[0] != provider
            }
            debug_log(f"Cleared capability evidence for {provider}", "providers")
