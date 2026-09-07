"""A pinned chat backend answers or the turn fails, with nothing else tried.

``chat_backend_override`` is not a preference. Naming a provider there is a
statement about which backend is allowed to answer, so the router stops
offering the rest of the chain as consolation: the pinned provider answers,
or the turn ends with a message saying that provider did not work. The one
thing that is not a fallback is the pinned provider answering in a shape it
supports, which is why a pin onto a backend with no native tool schema still
reaches the engine's text-based tool calling.

Automatic per-turn classification is unchanged and stays a soft preference,
tested in test_chat_backend_routing.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jarvis.llm import ProviderError, Tier, ToolsNotSupportedError
from jarvis.llm.route import Route, RoutedBackend
from jarvis.llm.route_state import RouteStateStore


def _route(name: str, *, provider: str = "openai_compatible",
           capabilities=("chat", "stream", "tools")) -> Route:
    return Route(
        name=name,
        provider=provider,
        base_url="https://example.invalid/v1",
        api_key="",
        model=f"{name}-model",
        tier=Tier.CHAT,
        timeout_sec=4.0,
        capabilities=frozenset(capabilities),
    )


class _Backend:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.chat_calls = 0

    def chat(self, model, messages, timeout_sec=30.0, extra_options=None,
             tools=None, thinking=False, on_token=None):
        self.chat_calls += 1
        if self.error:
            raise self.error
        return self.result


def _router(tmp_path: Path, routes, backends) -> RoutedBackend:
    state = RouteStateStore(tmp_path / "llm-routes-state.json")
    return RoutedBackend(routes, state_store=state,
                         backend_factory=backends.__getitem__)


class TestPinnedProviderNeverFallsBack:
    def test_pinned_provider_answers_and_no_other_route_is_tried(self, tmp_path):
        first = _route("first", provider="openai_compatible")
        pinned = _route("pinned", provider="claude_subscription")
        first_backend = _Backend(result={"message": {"content": "first"}})
        pinned_backend = _Backend(result={"message": {"content": "pinned"}})
        router = _router(tmp_path, [first, pinned],
                         {first: first_backend, pinned: pinned_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             forced_provider="claude_subscription")

        assert result == {"message": {"content": "pinned"}}
        assert first_backend.chat_calls == 0

    def test_a_failing_pinned_provider_ends_the_turn(self, tmp_path):
        """The whole point of the pin: a healthy alternative sitting right
        there in the chain must not rescue the turn, because answering from
        it would hide that the pinned backend is broken."""
        pinned = _route("pinned", provider="claude_subscription")
        healthy = _route("healthy", provider="openai_compatible")
        pinned_backend = _Backend(error=ProviderError("sidecar is down"))
        healthy_backend = _Backend(result={"message": {"content": "healthy"}})
        router = _router(tmp_path, [pinned, healthy],
                         {pinned: pinned_backend, healthy: healthy_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             forced_provider="claude_subscription")

        assert result is None
        assert pinned_backend.chat_calls == 1
        assert healthy_backend.chat_calls == 0

    def test_a_pinned_provider_with_no_route_ends_the_turn(self, tmp_path):
        """Pinning a provider that is not configured at all is a broken pin,
        not an invitation to answer from whatever else is around."""
        healthy = _route("healthy", provider="openai_compatible")
        healthy_backend = _Backend(result={"message": {"content": "healthy"}})
        router = _router(tmp_path, [healthy], {healthy: healthy_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             forced_provider="crew_chat")

        assert result is None
        assert healthy_backend.chat_calls == 0

    def test_a_disabled_pinned_route_ends_the_turn(self, tmp_path):
        pinned = Route(
            name="pinned", provider="claude_subscription",
            base_url="https://example.invalid/v1", api_key="",
            model="pinned-model", tier=Tier.CHAT, timeout_sec=4.0,
            enabled=False,
        )
        healthy = _route("healthy", provider="openai_compatible")
        healthy_backend = _Backend(result={"message": {"content": "healthy"}})
        router = _router(tmp_path, [pinned, healthy],
                         {pinned: _Backend(), healthy: healthy_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             forced_provider="claude_subscription")

        assert result is None
        assert healthy_backend.chat_calls == 0

    def test_every_route_of_the_pinned_provider_is_tried(self, tmp_path):
        """Not falling back means not leaving the pinned provider, not
        stopping at its first route: two routes onto the same backend are
        two ways of reaching the thing that was pinned."""
        broken = _route("broken", provider="openai_compatible")
        spare = _route("spare", provider="openai_compatible")
        broken_backend = _Backend(error=ProviderError("502"))
        spare_backend = _Backend(result={"message": {"content": "spare"}})
        router = _router(tmp_path, [broken, spare],
                         {broken: broken_backend, spare: spare_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             forced_provider="openai_compatible")

        assert result == {"message": {"content": "spare"}}
        assert broken_backend.chat_calls == 1

    def test_an_empty_reply_from_the_pinned_provider_ends_the_turn(self, tmp_path):
        pinned = _route("pinned", provider="claude_subscription")
        healthy = _route("healthy", provider="openai_compatible")
        healthy_backend = _Backend(result={"message": {"content": "healthy"}})
        router = _router(tmp_path, [pinned, healthy],
                         {pinned: _Backend(result={"message": {"content": ""}}),
                          healthy: healthy_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             forced_provider="claude_subscription")

        assert result is None
        assert healthy_backend.chat_calls == 0


class TestPinnedProviderAndTools:
    def test_a_pinned_provider_without_native_tools_asks_for_text_tools(self, tmp_path):
        """``claude_subscription`` advertises chat but not tools. Answering a
        tool-bearing call from a different provider would break the pin, and
        refusing outright would make every tool turn fail, so the router
        raises the signal the engine already handles by re-asking the same
        backend with markdown-fenced tools."""
        pinned = _route("pinned", provider="claude_subscription",
                        capabilities=("chat",))
        tool_capable = _route("tools", provider="openai_compatible")
        tool_backend = _Backend(result={"message": {"content": "tools"}})
        router = _router(tmp_path, [pinned, tool_capable],
                         {pinned: _Backend(), tool_capable: tool_backend})

        with pytest.raises(ToolsNotSupportedError):
            router.chat("chat", [{"role": "user", "content": "hi"}],
                        tools=[{"name": "weather"}],
                        forced_provider="claude_subscription")

        assert tool_backend.chat_calls == 0

    def test_a_pin_with_no_route_at_all_does_not_ask_for_text_tools(self, tmp_path):
        """Text-based tool calling is what a *present* backend does instead
        of a native schema. With nothing pinned in the chain there is no
        backend to re-ask, so the turn fails rather than looping."""
        tool_capable = _route("tools", provider="openai_compatible")
        router = _router(tmp_path, [tool_capable], {tool_capable: _Backend()})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             tools=[{"name": "weather"}],
                             forced_provider="crew_chat")

        assert result is None

    def test_a_pinned_tool_capable_provider_is_used_natively(self, tmp_path):
        pinned = _route("pinned", provider="openai_compatible")
        pinned_backend = _Backend(result={"message": {"content": "pinned"}})
        router = _router(tmp_path, [pinned], {pinned: pinned_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             tools=[{"name": "weather"}],
                             forced_provider="openai_compatible")

        assert result == {"message": {"content": "pinned"}}


class TestEngineWiring:
    """``chat_with_messages`` must send a manual override down the pinning
    path and automatic classification down the preference path, because the
    two mean different things to the router."""

    @staticmethod
    def _cfg(**overrides):
        base = SimpleNamespace(llm_chat_model="test-chat-model",
                               chat_backend_override="auto")
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    @pytest.fixture
    def mock_backend(self):
        backend = MagicMock()
        backend.chat.return_value = {"message": {"content": "ok"}}
        return backend

    def test_manual_override_is_sent_as_a_pin(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = self._cfg(chat_backend_override="claude_subscription")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

        kwargs = mock_backend.chat.call_args.kwargs
        assert kwargs["forced_provider"] == "claude_subscription"
        assert kwargs["preferred_provider"] is None

    def test_automatic_classification_is_sent_as_a_preference(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = self._cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}],
                               chat_backend_preference="complex")

        kwargs = mock_backend.chat.call_args.kwargs
        assert kwargs["forced_provider"] is None
        assert kwargs["preferred_provider"] == "claude_subscription"

    def test_a_pin_suppresses_automatic_classification(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = self._cfg(chat_backend_override="codex_subscription")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}],
                               chat_backend_preference="hermes")

        kwargs = mock_backend.chat.call_args.kwargs
        assert kwargs["forced_provider"] == "codex_subscription"
        assert kwargs["preferred_provider"] is None


class TestFailureMessage:
    """When a pinned backend produces nothing, the user is told which one
    and that nothing else was tried — telling them to rephrase would send
    them at a request that fails identically every time."""

    def test_the_message_names_the_pinned_provider(self):
        from src.jarvis.reply.engine import _no_reply_message

        message = _no_reply_message("chat", forced_provider="claude_subscription")

        assert "claude_subscription" in message

    def test_the_message_says_nothing_else_was_tried(self):
        from src.jarvis.reply.engine import _no_reply_message

        message = _no_reply_message("chat", forced_provider="crew_chat")

        assert message != _no_reply_message("chat")

    def test_a_pinned_tool_turn_names_the_pinned_provider_too(self):
        from src.jarvis.reply.engine import _no_reply_message

        message = _no_reply_message("tools", forced_provider="crew_chat")

        assert "crew_chat" in message

    def test_an_unpinned_turn_keeps_the_exhausted_chain_message(self):
        from src.jarvis.reply.engine import (
            NO_CHAT_BACKEND_MESSAGE,
            _no_reply_message,
        )

        assert _no_reply_message("chat") == NO_CHAT_BACKEND_MESSAGE
