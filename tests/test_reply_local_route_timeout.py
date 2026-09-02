"""Regression coverage for local reply timeouts in a routed configuration."""

from __future__ import annotations


class _AnswerAfterFourSecondsBackend:
    """Model double that exposes the effective timeout without sleeping."""

    def __init__(self) -> None:
        self.direct_timeouts: list[float] = []
        self.chat_timeouts: list[float] = []

    def direct(self, model, system_prompt, user_content, **kwargs):
        timeout = float(kwargs["timeout_sec"])
        self.direct_timeouts.append(timeout)
        return "none" if timeout > 4.0 else None

    def chat(self, model, messages, **kwargs):
        timeout = float(kwargs["timeout_sec"])
        self.chat_timeouts.append(timeout)
        if timeout <= 4.0:
            return None
        return {
            "message": {
                "role": "assistant",
                "content": "Mir geht es gut, danke der Nachfrage!",
            }
        }


def test_a_disabled_remote_route_does_not_force_local_greetings_into_fallback(
    mock_config, db, dialogue_memory,
):
    """A dormant route must not impose its four-second fallback cap locally."""
    from jarvis.llm.factory import get_llm_backend
    from jarvis.reply.engine import run_reply_engine

    mock_config.llm_routes = [{
        "name": "hermes",
        "provider": "openai_compatible",
        "base_url": "http://hermes.invalid/v1",
        "model": "hermes-agent",
        "tier": "chat",
        "timeout_sec": 120.0,
        "enabled": False,
        "capabilities": ["chat", "stream", "tools"],
    }]
    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.fast_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_tools_timeout_sec = 300.0
    mock_config.llm_chat_timeout_sec = 180.0
    mock_config.tool_selection_strategy = "llm"
    mock_config.planner_enabled = False
    mock_config.memory_digest_enabled = False
    mock_config.crew_handoff_enabled = False

    routed = get_llm_backend(mock_config)
    local = _AnswerAfterFourSecondsBackend()
    for route in routed.routes:
        if route.name in {"local-fast", "local-chat"}:
            routed._backends[route] = local

    reply = run_reply_engine(
        db=db,
        cfg=mock_config,
        tts=None,
        text="Hallo, wie gehts?",
        dialogue_memory=dialogue_memory,
        quiet=True,
    )

    assert reply == "Mir geht es gut, danke der Nachfrage!"
    assert local.direct_timeouts == [60.0]
    assert local.chat_timeouts == [180.0]
