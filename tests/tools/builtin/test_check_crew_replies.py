"""Behaviour tests for the checkCrewReplies tool.

Telegram gives no history endpoint, so the tool can only ever report what the
router already buffered while it was polling. These tests pin the argument
validation and the read-back of that buffer, never a network call.
"""

from unittest.mock import Mock, patch

import pytest

from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.builtin.ask_crew import AGENT_THREADS
from src.jarvis.tools.builtin.check_crew_replies import CheckCrewRepliesTool
from src.jarvis.tools.types import ToolErrorCode


class TestCheckCrewRepliesTool:
    def setup_method(self):
        self.tool = CheckCrewRepliesTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()
        self.context.cfg = Mock()
        self.context.cfg.telegram_bot_token = "tok"
        self.context.cfg.telegram_chat_id = "4242"
        self.context.cfg.telegram_api_base_url = ""
        self.context.cfg.crew_telegram_chat_id = "-100123"

    def _patch_router(self, router: Mock):
        return patch(
            "src.jarvis.tools.builtin.check_crew_replies.get_router",
            return_value=router,
        )

    def test_tool_properties(self):
        assert self.tool.name == "checkCrewReplies"
        assert set(self.tool.inputSchema["properties"]["agent"]["enum"]) == set(AGENT_THREADS)
        assert self.tool.inputSchema["required"] == ["agent"]

    def test_an_unknown_agent_is_rejected_without_touching_the_router(self):
        with self._patch_router(Mock()) as get_router:
            result = self.tool.run({"agent": "nobody"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_ARGUMENT.value
        get_router.assert_not_called()

    def test_missing_crew_chat_id_is_reported_without_touching_the_router(self):
        self.context.cfg.crew_telegram_chat_id = ""
        with self._patch_router(Mock()) as get_router:
            result = self.tool.run({"agent": "dev"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_CONFIG.value
        get_router.assert_not_called()

    def test_an_unavailable_router_is_reported_as_invalid_config(self):
        router = Mock()
        router.is_available = False
        with self._patch_router(router):
            result = self.tool.run({"agent": "dev"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_CONFIG.value

    def test_no_buffered_messages_yet(self):
        router = Mock()
        router.is_available = True
        router.get_topic_messages.return_value = []
        with self._patch_router(router):
            result = self.tool.run({"agent": "dev"}, self.context)

        assert result.success is True
        assert "no replies" in result.reply_text.lower()
        assert "dev" in result.reply_text.lower()

    def test_watches_the_agents_own_topic_before_reading(self):
        router = Mock()
        router.is_available = True
        router.get_topic_messages.return_value = []
        with self._patch_router(router):
            self.tool.run({"agent": "research"}, self.context)

        router.watch_topic.assert_called_once_with("-100123", AGENT_THREADS["research"])
        router.ensure_polling.assert_called_once()

    def test_jarvis_agent_checks_the_general_topic(self):
        router = Mock()
        router.is_available = True
        router.get_topic_messages.return_value = []
        with self._patch_router(router):
            self.tool.run({"agent": "jarvis"}, self.context)

        router.get_topic_messages.assert_called_once_with("-100123", None)

    def test_buffered_messages_are_returned_as_raw_lines(self):
        router = Mock()
        router.is_available = True
        router.get_topic_messages.return_value = [
            {"from": "Mission Control", "text": "Gesundheitscheck: ok", "date": 1},
            {"from": "Mission Control", "text": "Done with the audit", "date": 2},
        ]
        with self._patch_router(router):
            result = self.tool.run({"agent": "dev"}, self.context)

        assert result.success is True
        assert "Gesundheitscheck: ok" in result.reply_text
        assert "Done with the audit" in result.reply_text

    def test_agent_name_is_case_and_whitespace_insensitive(self):
        router = Mock()
        router.is_available = True
        router.get_topic_messages.return_value = []
        with self._patch_router(router):
            result = self.tool.run({"agent": "  DEV "}, self.context)

        assert result.success is True
