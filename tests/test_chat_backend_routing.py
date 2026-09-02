"""``chat_with_messages`` resolves which Tier.CHAT backend a turn prefers:
a manual config override, or (under "auto") the preference the tool
router's own LLM call already classified this turn as needing. Either
way the resolved preference is only ever a hint passed to
``RoutedBackend.chat(preferred_provider=...)`` — the existing route chain
and its fail-soft fallback are what actually enforce "never leave a turn
unanswered", tested separately in test_llm_routing.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _cfg(**overrides):
    base = SimpleNamespace(
        llm_chat_model="test-chat-model",
        chat_backend_override="auto",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


@pytest.fixture
def mock_backend():
    backend = MagicMock()
    backend.chat.return_value = {"message": {"content": "ok"}}
    return backend


class TestManualOverride:
    def test_override_forces_the_named_provider(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg(chat_backend_override="claude_subscription")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] == "claude_subscription"

    def test_override_forces_crew_chat(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg(chat_backend_override="crew_chat")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] == "crew_chat"

    def test_override_wins_over_automatic_preference(self, mock_backend):
        """Manual override is unconditional — it must not be overridden by
        whatever the router classified this turn as."""
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg(chat_backend_override="ollama")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="complex",
            )

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] == "ollama"

    def test_override_logs_the_decision(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg(chat_backend_override="claude_subscription")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend), \
             patch("src.jarvis.reply.engine.debug_log") as logged:
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

        assert any(
            "override" in str(call.args[0]).lower()
            and "claude_subscription" in str(call.args[0])
            for call in logged.call_args_list
        )


class TestAutomaticRouting:
    def test_complex_preference_selects_claude_subscription(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="complex",
            )

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] == "claude_subscription"

    def test_local_preference_selects_ollama(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="local",
            )

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] == "ollama"

    def test_hermes_preference_selects_crew_chat(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="hermes",
            )

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] == "crew_chat"

    def test_complex_preference_is_logged(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend), \
             patch("src.jarvis.reply.engine.debug_log") as logged:
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="complex",
            )

        assert any(
            "claude_subscription" in str(call.args[0])
            for call in logged.call_args_list
        )

    def test_hermes_preference_is_logged(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend), \
             patch("src.jarvis.reply.engine.debug_log") as logged:
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="hermes",
            )

        assert any(
            "crew_chat" in str(call.args[0])
            for call in logged.call_args_list
        )


class TestFailOpen:
    def test_no_preference_and_auto_override_leaves_chain_order_unchanged(self, mock_backend):
        """Classification unavailable this turn (timeout, disabled strategy,
        etc.) — the default 'auto' override with no preference must pass no
        preferred_provider at all, i.e. today's unmodified chain order."""
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] is None

    def test_missing_override_attribute_defaults_to_auto(self, mock_backend):
        """A cfg object built before this feature existed (no
        chat_backend_override attribute at all) must behave exactly like
        "auto" rather than raising."""
        from src.jarvis.reply.engine import chat_with_messages

        cfg = SimpleNamespace(llm_chat_model="test-chat-model")
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] is None

    def test_unrecognised_preference_value_leaves_chain_order_unchanged(self, mock_backend):
        from src.jarvis.reply.engine import chat_with_messages

        cfg = _cfg()
        with patch("src.jarvis.reply.engine.get_llm_backend", return_value=mock_backend):
            chat_with_messages(
                cfg, [{"role": "user", "content": "hi"}],
                chat_backend_preference="unexpected-value",
            )

        assert mock_backend.chat.call_args.kwargs["preferred_provider"] is None
