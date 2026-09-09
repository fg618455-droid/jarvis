"""Behaviour tests for the subscription-backed Claude LLM backend."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis.llm.claude_subscription_sidecar_client import ClaudeSidecarError


class FakeSidecar:
    def __init__(self, result="pong", error=None, chunks=()):
        self.result = result
        self.error = error
        self.chunks = list(chunks)
        self.requests: list[dict] = []

    def generate(self, model, system_prompt, prompt, timeout_sec, on_token=None):
        self.requests.append({
            "model": model,
            "system_prompt": system_prompt,
            "prompt": prompt,
            "timeout_sec": timeout_sec,
            "streaming": on_token is not None,
        })
        if self.error is not None:
            raise self.error
        if on_token is not None:
            for chunk in self.chunks:
                on_token(chunk)
        return self.result


def _backend(sidecar=None):
    from jarvis.llm.claude_subscription import ClaudeSubscriptionBackend

    return ClaudeSubscriptionBackend(sidecar or FakeSidecar())


class TestTextOnlyContract:
    def test_direct_returns_assembled_text(self):
        assert _backend().direct("model", "sys", "ping", timeout_sec=5.0) == "pong"

    def test_direct_sends_system_prompt_and_user_content_separately(self):
        sidecar = FakeSidecar()
        _backend(sidecar).direct("model", "be terse", "what is 2+2", timeout_sec=5.0)

        assert sidecar.requests == [{
            "model": "model",
            "system_prompt": "be terse",
            "prompt": "what is 2+2",
            "timeout_sec": 5.0,
            "streaming": False,
        }]

    def test_streaming_forwards_incremental_chunks_and_returns_full_text(self):
        chunks: list[str] = []
        result = _backend(FakeSidecar(chunks=["pon", "g"])).streaming(
            "model", "sys", "ping", on_token=chunks.append, timeout_sec=5.0
        )

        assert result == "pong"
        assert chunks == ["pon", "g"]

    def test_chat_returns_openai_shaped_message(self):
        result = _backend(FakeSidecar(result="hello")).chat(
            "model",
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            timeout_sec=5.0,
        )

        assert result["message"] == {"role": "assistant", "content": "hello"}
        assert result["choices"][0]["message"]["content"] == "hello"

    def test_chat_flattens_prior_turns_into_one_prompt(self):
        sidecar = FakeSidecar()
        messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow-up"},
        ]
        _backend(sidecar).chat("model", messages, timeout_sec=5.0)

        request = sidecar.requests[0]
        assert request["system_prompt"] == "sys prompt"
        assert "first question" in request["prompt"]
        assert "first answer" in request["prompt"]
        assert request["prompt"].strip().endswith("follow-up")

    def test_chat_with_native_tools_raises_before_the_sidecar_is_used(self):
        from jarvis.llm import ToolsNotSupportedError

        sidecar = FakeSidecar()
        with pytest.raises(ToolsNotSupportedError):
            _backend(sidecar).chat(
                "model",
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "ping"}}],
                timeout_sec=5.0,
            )
        assert sidecar.requests == []


class TestTypedFailures:
    @pytest.mark.parametrize("status,expected", [
        (401, "AuthError"),
        (403, "AuthError"),
        (404, "ModelUnavailableError"),
        (429, "RateLimitedError"),
        (500, "ProviderError"),
        (None, "ProviderError"),
    ])
    def test_sidecar_failures_map_to_the_public_typed_contract(self, status, expected):
        import jarvis.llm as llm

        sidecar = FakeSidecar(error=ClaudeSidecarError("sanitised", status=status))
        with pytest.raises(getattr(llm, expected)):
            _backend(sidecar).direct("model", "sys", "hi", timeout_sec=5.0)

    def test_auth_failure_is_logged_without_request_content(self):
        from jarvis.llm import AuthError

        sidecar = FakeSidecar(error=ClaudeSidecarError("sanitised", status=401))
        with patch("jarvis.llm.claude_subscription.debug_log") as logged:
            with pytest.raises(AuthError):
                _backend(sidecar).direct("model", "sys", "private prompt", timeout_sec=5.0)

        messages = [str(call.args[0]) for call in logged.call_args_list]
        assert any("auth/session failure" in message.lower() for message in messages)
        assert all("private prompt" not in message for message in messages)

    def test_unexpected_client_failure_is_sanitised_to_provider_error(self):
        from jarvis.llm import ProviderError

        sidecar = FakeSidecar(error=RuntimeError("secret path and credential"))
        with pytest.raises(ProviderError) as raised:
            _backend(sidecar).direct("model", "sys", "private prompt", timeout_sec=5.0)

        assert "secret" not in str(raised.value)
        assert "private prompt" not in str(raised.value)


class TestNeverThePrivateLane:
    @staticmethod
    def _settings(routes, **overrides):
        values = dict(
            llm_routes=routes,
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_claude_subscription_route_is_excluded_from_private_tier(self):
        from jarvis.llm import Tier, get_llm_backend

        cfg = self._settings([{
            "name": "claude-sub",
            "provider": "claude_subscription",
            "base_url": "claude-agent-sdk",
            "api_key": "",
            "model": "model",
            "tier": "chat",
            "timeout_sec": 30.0,
            "enabled": True,
        }, {
            "name": "private-attempt",
            "provider": "claude_subscription",
            "base_url": "claude-agent-sdk",
            "api_key": "",
            "model": "model",
            "tier": "private",
            "timeout_sec": 30.0,
            "enabled": True,
        }])
        backend = get_llm_backend(cfg)

        assert {route.provider for route in backend.routes_for(Tier.PRIVATE)} == {"ollama"}

    def test_claude_subscription_is_not_a_single_endpoint_provider(self):
        from jarvis.llm import Tier, get_llm_backend

        cfg = self._settings([], llm_provider="claude_subscription")
        assert get_llm_backend(cfg).routes_for(Tier.CHAT)[0].provider == "ollama"

    def test_claude_subscription_is_not_an_embedding_provider(self):
        from jarvis.llm import get_embedding_backend
        from jarvis.llm.ollama import OllamaBackend

        cfg = self._settings([], embedding_provider="claude_subscription")
        assert isinstance(get_embedding_backend(cfg), OllamaBackend)
