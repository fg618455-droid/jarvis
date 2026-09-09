from __future__ import annotations

import pytest

from jarvis.providers.base import Capabilities
from jarvis.providers.capabilities import ProviderCapabilityRegistry


@pytest.mark.unit
def test_unknown_provider_capabilities_fail_closed():
    registry = ProviderCapabilityRegistry()

    assert registry.get("claude") == Capabilities()


@pytest.mark.unit
def test_provider_capabilities_apply_when_no_model_evidence_exists():
    registry = ProviderCapabilityRegistry()
    transport = Capabilities(streaming=True, steering=True)

    registry.register("claude", transport)

    assert registry.get("claude") == transport
    assert registry.get("claude", "user-verified-model") == transport


@pytest.mark.unit
def test_model_capabilities_replace_provider_capabilities_when_known():
    registry = ProviderCapabilityRegistry()
    registry.register("codex", Capabilities(streaming=True, tools=True))
    model = Capabilities(streaming=True, images=True)

    registry.register("codex", model, model="model-1")

    assert registry.get("codex", "model-1") == model
    assert registry.get("codex", "model-2") == Capabilities(streaming=True, tools=True)


@pytest.mark.unit
def test_capability_evidence_is_isolated_by_provider():
    registry = ProviderCapabilityRegistry()
    registry.register("claude", Capabilities(mcp=True))

    assert registry.get("codex") == Capabilities()
    assert registry.get("hermes") == Capabilities()


@pytest.mark.unit
def test_invalid_provider_ids_are_rejected():
    registry = ProviderCapabilityRegistry()

    with pytest.raises(ValueError, match="other"):
        registry.register("other", Capabilities())
    with pytest.raises(ValueError, match="other"):
        registry.get("other")


@pytest.mark.unit
def test_blank_model_ids_are_rejected():
    registry = ProviderCapabilityRegistry()

    with pytest.raises(ValueError, match="model"):
        registry.register("codex", Capabilities(), model="")


@pytest.mark.unit
def test_clear_removes_only_the_requested_provider_evidence():
    registry = ProviderCapabilityRegistry()
    registry.register("claude", Capabilities(streaming=True))
    registry.register("codex", Capabilities(tools=True), model="model-1")

    registry.clear("claude")

    assert registry.get("claude") == Capabilities()
    assert registry.get("codex", "model-1") == Capabilities(tools=True)
