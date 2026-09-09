"""The memory write lane and vector space never leave loopback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _cloud_cfg():
    return SimpleNamespace(
        llm_routes=[
            {
                "name": "configured-cloud",
                "provider": "openai_compatible",
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic-credential",
                "model": "cloud-model",
                "tier": "chat",
                "timeout_sec": 4,
            },
            {
                "name": "configured-fast-cloud",
                "provider": "openai_compatible",
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic-credential",
                "model": "fast-cloud-model",
                "tier": "fast",
                "timeout_sec": 4,
            },
        ],
        llm_provider="openai_compatible",
        llm_base_url="https://example.invalid/v1",
        llm_api_key="synthetic-credential",
        llm_chat_model="cloud-model",
        fast_model="fast-cloud-model",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="local-private-model",
        ollama_embed_model="local-embedding-model",
        embedding_provider="openai_compatible",
        embedding_base_url="https://example.invalid/v1",
        embedding_api_key="synthetic-credential",
        embedding_model="remote-embedding-model",
    )


@pytest.mark.parametrize("method", ["direct", "streaming"])
def test_private_tier_constructs_only_loopback_ollama(method):
    from jarvis.llm import Tier, get_llm_backend, resolve_model
    from jarvis.llm.ollama import OllamaBackend

    cfg = _cloud_cfg()
    model = resolve_model(cfg, Tier.PRIVATE)
    backend = get_llm_backend(cfg)

    with patch("jarvis.llm.route.OpenAICompatibleBackend") as cloud, \
         patch.object(OllamaBackend, method, return_value="local") as local:
        if method == "direct":
            result = backend.direct(model, "system", "user")
        else:
            result = backend.streaming(model, "system", "user")

    assert result == "local"
    cloud.assert_not_called()
    assert local.call_args.args[0] == "local-private-model"
    assert backend.routes_for(Tier.PRIVATE)[0].base_url.startswith("http://127.0.0.1:")


def test_embedding_backend_is_local_ollama_and_never_routed():
    from jarvis.llm import RoutedBackend, get_embedding_backend
    from jarvis.llm.ollama import OllamaBackend

    backend = get_embedding_backend(_cloud_cfg())

    assert isinstance(backend, OllamaBackend)
    assert not isinstance(backend, RoutedBackend)
    assert backend.base_url.startswith("http://127.0.0.1:")


@pytest.mark.parametrize(
    "context",
    ["diary summary", "deflection rewrite", "topic optimisation"],
)
def test_conversation_memory_writes_mark_the_private_lane(context):
    from jarvis.llm import Tier
    from jarvis.memory import conversation

    cfg = _cloud_cfg()
    with patch.object(conversation, "get_llm_backend") as factory:
        factory.return_value.direct.return_value = "local"
        assert conversation._direct_llm(cfg, "system", context) == "local"

    model = factory.return_value.direct.call_args.args[0]
    assert model.tier is Tier.PRIVATE


@pytest.mark.parametrize("context", ["graph extraction", "graph node merge"])
def test_graph_memory_writes_mark_the_private_lane(context):
    from jarvis.llm import Tier
    from jarvis.memory import graph_ops

    cfg = _cloud_cfg()
    with patch.object(graph_ops, "call_llm_direct", return_value="NONE") as call:
        if context == "graph extraction":
            graph_ops.extract_graph_memories("summary", cfg, "cloud-chat")
        else:
            node = SimpleNamespace(data="existing fact", name="User", description="")
            store = SimpleNamespace(get_node=lambda _node_id: node)
            graph_ops.merge_node_data(
                store, "node", ["new fact"], cfg, "cloud-chat", node=node
            )

    assert call.call_args.kwargs["chat_model"].tier is Tier.PRIVATE
