"""Behaviour tests for the askCrew tool.

The tool's only job is to post a task into the right Telegram topic of the
Hermes crew's group and return — it never waits for or parses a reply, so
the interesting behaviour is entirely about what gets sent and when the
tool refuses to send anything at all.
"""

from unittest.mock import Mock, patch

import pytest
import requests

from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.builtin.ask_crew import AGENT_THREADS, AskCrewTool
from src.jarvis.tools.types import ToolErrorCode


class TestAskCrewTool:
    def setup_method(self):
        self.tool = AskCrewTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()
        self.context.cfg = Mock()
        self.context.cfg.telegram_bot_token = "tok"
        self.context.cfg.crew_telegram_chat_id = "-100123"
        self.context.cfg.telegram_api_base_url = ""

    def test_tool_properties(self):
        assert self.tool.name == "askCrew"
        assert set(self.tool.inputSchema["properties"]["agent"]["enum"]) == set(AGENT_THREADS)
        assert self.tool.inputSchema["required"] == ["agent", "task"]

    def test_an_unknown_agent_is_rejected_without_a_network_call(self):
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport"
        ) as transport_cls:
            result = self.tool.run({"agent": "nobody", "task": "x"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_ARGUMENT.value
        transport_cls.assert_not_called()

    def test_an_empty_task_is_rejected(self):
        result = self.tool.run({"agent": "dev", "task": "   "}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_ARGUMENT.value

    def test_missing_chat_id_is_reported_without_a_network_call(self):
        self.context.cfg.crew_telegram_chat_id = ""
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport"
        ) as transport_cls:
            result = self.tool.run({"agent": "dev", "task": "fix the router"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_CONFIG.value
        transport_cls.assert_not_called()

    def test_missing_bot_token_is_reported_without_a_network_call(self):
        self.context.cfg.telegram_bot_token = ""
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport"
        ) as transport_cls:
            result = self.tool.run({"agent": "dev", "task": "fix the router"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.INVALID_CONFIG.value
        transport_cls.assert_not_called()

    def test_a_successful_send_posts_to_the_agents_own_topic(self):
        transport = Mock()
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport",
            return_value=transport,
        ):
            result = self.tool.run(
                {"agent": "research", "task": "check tomorrow's timetable"}, self.context,
            )

        assert result.success is True
        transport.post.assert_called_once()
        method, payload = transport.post.call_args.args[0], transport.post.call_args.args[1]
        assert method == "sendMessage"
        assert payload["chat_id"] == "-100123"
        assert payload["text"] == "check tomorrow's timetable"
        assert payload["message_thread_id"] == AGENT_THREADS["research"]

    def test_jarvis_agent_has_no_thread_id_in_the_payload(self):
        transport = Mock()
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport",
            return_value=transport,
        ):
            self.tool.run({"agent": "jarvis", "task": "status update"}, self.context)

        payload = transport.post.call_args.args[1]
        assert "message_thread_id" not in payload

    def test_a_network_failure_is_reported_as_retryable(self):
        transport = Mock()
        transport.post.side_effect = requests.exceptions.ConnectionError("no route")
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport",
            return_value=transport,
        ):
            result = self.tool.run({"agent": "dev", "task": "x"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.UNAVAILABLE.value
        assert result.retryable is True

    def test_a_rejected_request_is_reported_as_retryable(self):
        transport = Mock()
        transport.post.side_effect = RuntimeError("Telegram Bot API rejected the request")
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport",
            return_value=transport,
        ):
            result = self.tool.run({"agent": "dev", "task": "x"}, self.context)

        assert result.success is False
        assert result.error_code == ToolErrorCode.UNAVAILABLE.value
        assert result.retryable is True

    def test_agent_name_is_case_and_whitespace_insensitive(self):
        transport = Mock()
        with patch(
            "src.jarvis.tools.builtin.ask_crew.RequestsTelegramTransport",
            return_value=transport,
        ):
            result = self.tool.run({"agent": "  DEV ", "task": "x"}, self.context)

        assert result.success is True
