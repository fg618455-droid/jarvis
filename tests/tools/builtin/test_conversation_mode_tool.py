"""Asking for conversation mode should turn it on, not be answered about.

"Konversationsmodus on" reached the reply engine and came back as prose,
because the only path that could act on it was the intent judge, and the
judge is not always there: it can be unavailable, and text chat and
Telegram have no judge at all. A control phrase that falls through to the
reply engine is a control phrase the assistant discusses instead of obeys.

The router already understands any language the model does, so the switch
belongs in the tool layer rather than behind a list of phrases nobody can
maintain in every language the assistant supports.
"""

from unittest.mock import Mock, patch

import pytest

from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.builtin.conversation_mode import ConversationModeTool
from src.jarvis.tools.registry import BUILTIN_TOOLS
from src.jarvis.tools.types import ToolErrorCode


class TestConversationModeTool:
    def setup_method(self):
        self.tool = ConversationModeTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()
        self.context.cfg = Mock()

    def test_it_is_registered_as_a_builtin(self):
        assert isinstance(BUILTIN_TOOLS.get("setConversationMode"), ConversationModeTool)

    def test_the_schema_carries_no_language_specific_phrasing(self):
        """The model maps the user's words, in whatever language, onto a
        boolean. Nothing here may name a phrase in any one language."""
        schema = self.tool.inputSchema

        assert schema["properties"]["enabled"]["type"] == "boolean"
        assert schema["required"] == ["enabled"]

    def test_turning_it_on_reaches_the_listener(self):
        with patch(
            "src.jarvis.tools.builtin.conversation_mode.set_conversation_mode",
            return_value=True,
        ) as switch:
            result = self.tool.run({"enabled": True}, self.context)

        assert result.success is True
        switch.assert_called_once_with(True)

    def test_turning_it_off_reaches_the_listener(self):
        with patch(
            "src.jarvis.tools.builtin.conversation_mode.set_conversation_mode",
            return_value=True,
        ) as switch:
            result = self.tool.run({"enabled": False}, self.context)

        assert result.success is True
        switch.assert_called_once_with(False)

    def test_a_missing_argument_is_rejected_without_touching_the_switch(self):
        """Guessing which way to flip a switch the user asked about is
        worse than saying the request was not understood."""
        with patch(
            "src.jarvis.tools.builtin.conversation_mode.set_conversation_mode",
        ) as switch:
            result = self.tool.run({}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_ARGUMENT
        switch.assert_not_called()

    def test_no_listener_is_reported_rather_than_claimed_as_done(self):
        """Text chat has no microphone to open. Reporting success would
        tell the user a mode is running that nothing is running."""
        with patch(
            "src.jarvis.tools.builtin.conversation_mode.set_conversation_mode",
            return_value=False,
        ):
            result = self.tool.run({"enabled": True}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.UNAVAILABLE

    def test_the_result_states_which_way_the_switch_went(self):
        """The model writes the sentence the user hears, so it needs the
        state in the result rather than having to remember the request."""
        with patch(
            "src.jarvis.tools.builtin.conversation_mode.set_conversation_mode",
            return_value=True,
        ):
            on = self.tool.run({"enabled": True}, self.context)
            off = self.tool.run({"enabled": False}, self.context)

        assert "on" in on.reply_text.lower()
        assert "off" in off.reply_text.lower()
