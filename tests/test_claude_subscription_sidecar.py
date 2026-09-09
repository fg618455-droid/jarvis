"""Safety behaviour enforced by the Claude Agent SDK sidecar."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


SIDECAR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "jarvis"
    / "llm"
    / "claude_subscription_sidecar.py"
)


def _load_sidecar(fake_sdk):
    previous = sys.modules.get("claude_agent_sdk")
    sys.modules["claude_agent_sdk"] = fake_sdk
    try:
        spec = importlib.util.spec_from_file_location("tested_claude_sidecar", SIDECAR)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("claude_agent_sdk", None)
        else:
            sys.modules["claude_agent_sdk"] = previous


def test_every_session_has_the_deny_all_callback_and_default_permission_mode():
    captured: list[dict] = []

    def options_factory(**kwargs):
        captured.append(kwargs)
        return types.SimpleNamespace(**kwargs)

    class Client:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            denial = await self.options.can_use_tool(
                "account_connector", {"secret": "must not be logged"}, None
            )
            assert denial.kind == "deny"

        async def receive_response(self):
            result_type = type("ResultMessage", (), {})
            result = result_type()
            result.is_error = False
            yield result

        async def disconnect(self):
            return None

    fake_sdk = types.SimpleNamespace(
        ClaudeAgentOptions=options_factory,
        ClaudeSDKClient=Client,
        PermissionResultDeny=lambda message: types.SimpleNamespace(kind="deny", message=message),
    )
    sidecar = _load_sidecar(fake_sdk)

    emitted: list[dict] = []
    for request_id in (1, 2):
        asyncio.run(
            sidecar._generate(
                {
                    "id": request_id,
                    "model": "model",
                    "system_prompt": "system",
                    "prompt": "prompt",
                    "stream": False,
                },
                emitted.append,
            )
        )

    assert len(captured) == 2
    for options in captured:
        assert options["tools"] == []
        assert options["setting_sources"] == []
        assert options["mcp_servers"] == {}
        assert options["permission_mode"] == "default"
        assert options["max_turns"] == 1
        assert callable(options["can_use_tool"])
    assert [message["type"] for message in emitted].count("tool_denied") == 2
    assert "must not be logged" not in json.dumps(emitted)
