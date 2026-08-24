"""Behaviour tests for :class:`ClaudeSubscriptionBackend`.

This backend rides an authenticated Claude Code CLI session
(``claude_agent_sdk``) instead of a metered API key. It must never be able
to act as a second agentic loop, so the load-bearing assertions here pin
the exact session options constructed (mirroring how
``test_open_on_computer.py`` asserts the exact ``Popen`` vector rather
than "it succeeded") and the exact typed failure raised for every
auth/session error shape the SDK can produce.

``claude_agent_sdk`` is an optional dependency (see the module docstring
in ``jarvis/llm/claude_subscription.py`` for why) and is not installed in
the test environment, so every test here patches
``jarvis.llm.claude_subscription._sdk`` with a lightweight fake exposing
just the surface the backend uses.
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest


class FakeClaudeSDKError(Exception):
    """Stand-in for claude_agent_sdk.ClaudeSDKError."""


class FakeResultError(FakeClaudeSDKError):
    """Stand-in for claude_agent_sdk.ResultError."""

    def __init__(self, message, api_error_status=None):
        super().__init__(message)
        self.api_error_status = api_error_status


class FakeCLINotFoundError(FakeClaudeSDKError):
    """Stand-in for claude_agent_sdk.CLINotFoundError."""


def _content_block(text):
    return types.SimpleNamespace(text=text)


def _assistant_message(text):
    return _typed("AssistantMessage", content=[_content_block(text)])


def _typed(class_name, **attrs):
    """Build an object whose ``type(...).__name__`` matches ``class_name``,
    since the backend dispatches on that rather than isinstance checks
    (it never imports the SDK's message classes directly)."""
    cls = type(class_name, (), {})
    obj = cls()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _result_message(is_error=False, api_error_status=None):
    return _typed("ResultMessage", is_error=is_error, api_error_status=api_error_status)


def _stream_event(text_delta):
    return _typed(
        "StreamEvent",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text_delta},
        },
    )


class FakeClient:
    """Stand-in for claude_agent_sdk.ClaudeSDKClient.

    Records the options it was constructed with and the prompt it was
    queried with, and yields a scripted sequence of messages from
    ``receive_response()``. If ``raise_on`` is set, raises that exception
    from the named lifecycle method instead of proceeding.
    """

    instances: list["FakeClient"] = []

    def __init__(self, options, messages=(), raise_on=None):
        self.options = options
        self._messages = list(messages)
        self._raise_on = raise_on or {}
        self.queried_with = None
        self.connected = False
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self):
        if "connect" in self._raise_on:
            raise self._raise_on["connect"]
        self.connected = True

    async def query(self, prompt):
        if "query" in self._raise_on:
            raise self._raise_on["query"]
        self.queried_with = prompt

    async def receive_response(self):
        if "receive_response" in self._raise_on:
            raise self._raise_on["receive_response"]
        for message in self._messages:
            yield message

    async def disconnect(self):
        self.disconnected = True


def _make_fake_sdk(messages=(), raise_on=None):
    """Build a fake ``claude_agent_sdk`` module namespace and return it
    alongside the captured ``ClaudeAgentOptions`` kwargs dict and the
    client class used, so a test can both drive behaviour and assert on
    exactly what was constructed."""
    captured_options: dict = {}

    def _options_factory(**kwargs):
        captured_options.update(kwargs)
        return types.SimpleNamespace(**kwargs)

    def _client_factory(options):
        return FakeClient(options, messages=messages, raise_on=raise_on)

    fake = types.SimpleNamespace(
        ClaudeAgentOptions=_options_factory,
        ClaudeSDKClient=_client_factory,
        PermissionResultDeny=lambda message: types.SimpleNamespace(
            kind="deny", message=message
        ),
        ClaudeSDKError=FakeClaudeSDKError,
        ResultError=FakeResultError,
        CLINotFoundError=FakeCLINotFoundError,
    )
    return fake, captured_options


@pytest.fixture(autouse=True)
def _reset_client_instances():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


def _backend():
    from jarvis.llm.claude_subscription import ClaudeSubscriptionBackend

    return ClaudeSubscriptionBackend()


class TestSessionNeverExecutesTools:
    """The SDK session must be able to generate text only. This is the
    load-bearing safety property of the whole backend."""

    def test_session_options_strip_every_tool_surface(self):
        fake, captured = _make_fake_sdk(messages=[_result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            _backend().direct("claude-sonnet-4-5-20250929", "system", "hi", timeout_sec=5.0)

        assert captured["tools"] == []
        assert captured["setting_sources"] == []
        assert captured["mcp_servers"] == {}
        assert captured["permission_mode"] == "default"
        assert captured["max_turns"] == 1
        assert callable(captured["can_use_tool"])

    def test_permission_mode_is_never_bypass_permissions(self):
        fake, captured = _make_fake_sdk(messages=[_result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            _backend().direct("claude-sonnet-4-5-20250929", "system", "hi", timeout_sec=5.0)

        # bypassPermissions (and other auto-approving modes) skip
        # can_use_tool entirely per the SDK's own CanUseToolShadowedWarning
        # ("can_use_tool is set but some tool calls are auto-approved
        # before it runs") — using one here would silently defeat the
        # deny-all gate below.
        assert captured["permission_mode"] == "default"

    def test_can_use_tool_callback_denies_every_attempt(self):
        import asyncio

        fake, captured = _make_fake_sdk(messages=[_result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake), \
             patch("jarvis.llm.claude_subscription.debug_log") as logged:
            _backend().direct("claude-sonnet-4-5-20250929", "system", "hi", timeout_sec=5.0)
            deny_all = captured["can_use_tool"]
            result = asyncio.run(
                deny_all("mcp__composio__COMPOSIO_REMOTE_BASH_TOOL", {"command": "ls"}, None)
            )

        assert result.kind == "deny"
        assert any(
            "denied" in str(call.args[0]).lower()
            and "COMPOSIO_REMOTE_BASH_TOOL" in str(call.args[0])
            for call in logged.call_args_list
        )


class TestTextOnlyContract:
    def test_direct_returns_assembled_text(self):
        fake, _ = _make_fake_sdk(messages=[_assistant_message("pong"), _result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            result = _backend().direct("claude-sonnet-4-5-20250929", "sys", "ping", timeout_sec=5.0)
        assert result == "pong"

    def test_direct_sends_system_prompt_and_user_content_separately(self):
        fake, captured = _make_fake_sdk(messages=[_result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            _backend().direct("claude-sonnet-4-5-20250929", "be terse", "what is 2+2", timeout_sec=5.0)

        assert captured["system_prompt"] == "be terse"
        assert FakeClient.instances[0].queried_with == "what is 2+2"

    def test_streaming_forwards_incremental_chunks_and_returns_full_text(self):
        fake, captured = _make_fake_sdk(messages=[
            _stream_event("pon"),
            _stream_event("g"),
            _assistant_message("pong"),
            _result_message(),
        ])
        chunks = []
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            result = _backend().streaming(
                "claude-sonnet-4-5-20250929", "sys", "ping",
                on_token=chunks.append, timeout_sec=5.0,
            )
        assert result == "pong"
        assert chunks == ["pon", "g"]
        assert captured["include_partial_messages"] is True

    def test_chat_returns_openai_shaped_message(self):
        fake, _ = _make_fake_sdk(messages=[_assistant_message("hello"), _result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            result = _backend().chat(
                "claude-sonnet-4-5-20250929",
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                timeout_sec=5.0,
            )
        assert result["message"] == {"role": "assistant", "content": "hello"}
        assert result["choices"][0]["message"]["content"] == "hello"

    def test_chat_flattens_prior_turns_into_one_prompt(self):
        fake, captured = _make_fake_sdk(messages=[_result_message()])
        messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow-up"},
        ]
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            _backend().chat("claude-sonnet-4-5-20250929", messages, timeout_sec=5.0)

        assert captured["system_prompt"] == "sys prompt"
        prompt = FakeClient.instances[0].queried_with
        assert "first question" in prompt
        assert "first answer" in prompt
        assert prompt.strip().endswith("follow-up")

    def test_chat_with_native_tools_raises_tools_not_supported_without_touching_sdk(self):
        from jarvis.llm import ToolsNotSupportedError

        fake, _ = _make_fake_sdk(messages=[_result_message()])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            with pytest.raises(ToolsNotSupportedError):
                _backend().chat(
                    "claude-sonnet-4-5-20250929",
                    [{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "ping"}}],
                    timeout_sec=5.0,
                )
        assert FakeClient.instances == []


class TestTypedFailures:
    @pytest.mark.parametrize("status,expected", [
        (401, "AuthError"),
        (403, "AuthError"),
        (404, "ModelUnavailableError"),
        (429, "RateLimitedError"),
    ])
    def test_result_message_error_maps_to_typed_failure(self, status, expected):
        import jarvis.llm as llm

        fake, _ = _make_fake_sdk(messages=[_result_message(is_error=True, api_error_status=status)])
        expected_cls = getattr(llm, expected)
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            with pytest.raises(expected_cls):
                _backend().direct("claude-sonnet-4-5-20250929", "sys", "hi", timeout_sec=5.0)

    def test_unrecognised_error_status_maps_to_generic_provider_error(self):
        from jarvis.llm import ProviderError

        fake, _ = _make_fake_sdk(messages=[_result_message(is_error=True, api_error_status=500)])
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            with pytest.raises(ProviderError):
                _backend().direct("claude-sonnet-4-5-20250929", "sys", "hi", timeout_sec=5.0)

    def test_raised_result_error_maps_to_typed_failure_and_logs_session_failure(self):
        from jarvis.llm import AuthError

        fake, _ = _make_fake_sdk(raise_on={
            "receive_response": FakeResultError("session expired", api_error_status=401)
        })
        with patch("jarvis.llm.claude_subscription._sdk", fake), \
             patch("jarvis.llm.claude_subscription.debug_log") as logged:
            with pytest.raises(AuthError):
                _backend().direct("claude-sonnet-4-5-20250929", "sys", "hi", timeout_sec=5.0)

        assert any(
            "auth/session failure" in str(call.args[0]).lower()
            for call in logged.call_args_list
        )

    def test_cli_not_found_maps_to_provider_error(self):
        from jarvis.llm import ProviderError

        fake, _ = _make_fake_sdk(raise_on={"connect": FakeCLINotFoundError("no claude on PATH")})
        with patch("jarvis.llm.claude_subscription._sdk", fake):
            with pytest.raises(ProviderError):
                _backend().direct("claude-sonnet-4-5-20250929", "sys", "hi", timeout_sec=5.0)

    def test_sdk_not_installed_raises_provider_error(self):
        from jarvis.llm import ProviderError

        with patch("jarvis.llm.claude_subscription._sdk", None):
            with pytest.raises(ProviderError):
                _backend().direct("claude-sonnet-4-5-20250929", "sys", "hi", timeout_sec=5.0)

    def test_timeout_raises_provider_error(self):
        import asyncio

        class HangingClient(FakeClient):
            async def receive_response(self):
                await asyncio.sleep(10)
                if False:
                    yield None  # pragma: no cover - keeps this an async generator

        fake, _ = _make_fake_sdk()
        fake.ClaudeSDKClient = lambda options: HangingClient(options)
        from jarvis.llm import ProviderError

        with patch("jarvis.llm.claude_subscription._sdk", fake):
            with pytest.raises(ProviderError):
                _backend().direct("claude-sonnet-4-5-20250929", "sys", "hi", timeout_sec=0.05)


class TestNeverThePrivateLane:
    def test_claude_subscription_route_is_excluded_from_private_tier(self):
        from types import SimpleNamespace
        from jarvis.llm import Tier, get_llm_backend

        cfg = SimpleNamespace(
            llm_routes=[{
                "name": "claude-sub",
                "provider": "claude_subscription",
                "base_url": "claude-agent-sdk",
                "api_key": "",
                "model": "claude-sonnet-4-5-20250929",
                "tier": "chat",
                "timeout_sec": 30.0,
                "enabled": True,
            }, {
                "name": "claude-sub-private-attempt",
                "provider": "claude_subscription",
                "base_url": "claude-agent-sdk",
                "api_key": "",
                "model": "claude-sonnet-4-5-20250929",
                "tier": "private",
                "timeout_sec": 30.0,
                "enabled": True,
            }],
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
        )
        backend = get_llm_backend(cfg)

        private_providers = {route.provider for route in backend.routes_for(Tier.PRIVATE)}
        assert private_providers == {"ollama"}

    def test_claude_subscription_is_not_selectable_as_the_single_endpoint_provider(self):
        from types import SimpleNamespace
        from jarvis.llm import Tier, get_llm_backend

        cfg = SimpleNamespace(
            llm_routes=[],
            llm_provider="claude_subscription",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
        )
        backend = get_llm_backend(cfg)

        chat_route = backend.routes_for(Tier.CHAT)[0]
        assert chat_route.provider == "ollama"

    def test_claude_subscription_is_not_selectable_as_the_embedding_provider(self):
        from types import SimpleNamespace
        from jarvis.llm import get_embedding_backend
        from jarvis.llm.ollama import OllamaBackend

        cfg = SimpleNamespace(
            llm_routes=[],
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
            embedding_provider="claude_subscription",
        )
        backend = get_embedding_backend(cfg)
        assert isinstance(backend, OllamaBackend)
