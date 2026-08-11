"""The three-lane model routing system.

Jarvis assigns every completion context to one route lane:

- ``Tier.FAST`` — the small, warm, low-latency model behind real-time
  classification passes. These contexts take a few thousand tokens in and
  emit tiny strict-JSON answers, so latency dominates and a ~2B model is
  ideal.
- ``Tier.CHAT``: the capable route chain that writes replies and plans.
- ``Tier.PRIVATE``: one loopback Ollama model for memory writes and graph
  mutations.

The Model tiers table in ``llm.spec.md`` is the authoritative list of
which context runs on which tier; docstrings elsewhere point here rather
than re-enumerating it.

FAST and CHAT fields are resolved at config load. PRIVATE always reads
``ollama_chat_model``. The returned value remains a string while carrying
its lane for `RoutedBackend`.
"""

from __future__ import annotations

from enum import Enum


class Tier(Enum):
    """Which route chain an LLM context uses."""

    FAST = "fast"
    CHAT = "chat"
    PRIVATE = "private"


class ResolvedModel(str):
    """A model name carrying its route lane while remaining a normal string."""

    tier: Tier

    def __new__(cls, value: str, tier: Tier):
        instance = super().__new__(cls, value)
        instance.tier = tier
        return instance


def resolve_model(cfg, tier: Tier) -> ResolvedModel:
    """Return the model name for ``tier`` under the active settings.

    ``Tier.FAST`` reads ``cfg.fast_model``; ``Tier.CHAT`` reads
    ``cfg.llm_chat_model``; ``Tier.PRIVATE`` reads ``cfg.ollama_chat_model``.
    An empty fast model (possible only on
    hand-built cfg objects — config load always resolves it) falls back
    to the chat model so a context never receives an empty name.
    """
    chat = str(getattr(cfg, "llm_chat_model", "") or "").strip()
    if tier is Tier.FAST:
        value = str(getattr(cfg, "fast_model", "") or "").strip() or chat
    elif tier is Tier.PRIVATE:
        value = str(getattr(cfg, "ollama_chat_model", "") or "").strip() or chat
    else:
        value = chat
    return ResolvedModel(value, tier)
