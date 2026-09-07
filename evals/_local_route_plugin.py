"""Give the eval harness a chain that can actually reach a model.

`MockConfig` carries no `llm_routes`, and FAST/CHAT are exactly what
`llm_routes` configures, so every eval that needs a model resolves to an
empty chain and fails before reaching one. This plugin points both tiers
at the local Ollama server through its OpenAI-compatible endpoint, which
is a provider the route chain accepts.

Load it explicitly, so it never affects an ordinary run:

    pytest evals/... -p _local_route_plugin
"""

from __future__ import annotations

import os

import helpers

_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "gemma4:e2b")
_BASE_URL = os.environ.get("EVAL_LOCAL_BASE_URL", "http://127.0.0.1:11434/v1")


def _route(tier: str) -> dict:
    return {
        "name": f"local-{tier}",
        "provider": "openai_compatible",
        "base_url": _BASE_URL,
        "api_key": "",
        "api_key_env": "",
        "model": _MODEL,
        "tier": tier,
        "timeout_sec": 120.0,
        "enabled": True,
        "capabilities": ["chat", "stream", "tools"],
    }


helpers.MockConfig.llm_routes = [_route("fast"), _route("chat")]
helpers.MockConfig.chat_backend_override = "auto"
