"""Unit tests for the toolSearchTool builtin."""

from unittest.mock import patch

import pytest

from jarvis.tools.builtin.tool_search import ToolSearchTool
from jarvis.tools.base import ToolContext


def _ctx(cfg, deadline=None):
    return ToolContext(
        db=None,
        cfg=cfg,
        system_prompt="",
        original_prompt="",
        redacted_text="",
        max_retries=0,
        user_print=lambda _m: None,
        language=None,
        deadline=deadline,
    )


class TestToolSearchTool:
    def test_rejects_missing_query(self, mock_config):
        tool = ToolSearchTool()
        result = tool.run({}, _ctx(mock_config))
        assert result.success is False
        assert "query" in (result.error_message or "").lower()

    def test_invokes_select_tools_and_formats_list(self, mock_config):
        tool = ToolSearchTool()
        with patch(
            "jarvis.tools.builtin.tool_search.select_tools",
            return_value=["webSearch", "stop", "toolSearchTool", "getWeather"],
        ) as mock_sel:
            result = tool.run({"query": "look up a fact"}, _ctx(mock_config))
        assert mock_sel.called
        assert result.success is True
        text = result.reply_text or ""
        # Sentinel and self are filtered out; real tools appear as
        # `name: description`.
        assert "webSearch" in text
        assert "getWeather" in text
        assert "stop" not in text.split("\n")[0]
        assert "toolSearchTool" not in text.splitlines()[0]
        # Each line has the colon-joined description format.
        for line in text.splitlines():
            assert ":" in line or line.strip() in ("webSearch", "getWeather")

    def test_empty_result_returns_honest_note(self, mock_config):
        tool = ToolSearchTool()
        with patch(
            "jarvis.tools.builtin.tool_search.select_tools",
            return_value=["stop", "toolSearchTool"],
        ):
            result = tool.run({"query": "do something"}, _ctx(mock_config))
        assert result.success is True
        assert "no additional tools" in (result.reply_text or "").lower()

    def test_select_tools_exception_returns_error(self, mock_config):
        tool = ToolSearchTool()
        with patch(
            "jarvis.tools.builtin.tool_search.select_tools",
            side_effect=RuntimeError("router down"),
        ):
            result = tool.run({"query": "x"}, _ctx(mock_config))
        assert result.success is False
        assert "router down" in (result.error_message or "")

    def test_bounds_llm_and_embed_timeouts_to_the_remaining_reply_deadline(
        self, mock_config,
    ):
        """A hung router or embedding call inside toolSearchTool must not
        outlive the reply turn just because llm_tools_timeout_sec /
        llm_embedding_timeout_sec are configured far larger (300s / 10s+)
        than what's actually left of the turn."""
        from jarvis.llm.route import RequestDeadline

        mock_config.llm_tools_timeout_sec = 300.0
        mock_config.llm_embedding_timeout_sec = 300.0
        deadline = RequestDeadline.after(2.0)

        tool = ToolSearchTool()
        with patch(
            "jarvis.tools.builtin.tool_search.select_tools",
            return_value=["webSearch"],
        ) as mock_sel:
            tool.run({"query": "x"}, _ctx(mock_config, deadline=deadline))

        _, kwargs = mock_sel.call_args
        assert 0 < kwargs["llm_timeout_sec"] <= 2.0
        assert 0 < kwargs["embed_timeout_sec"] <= 2.0

    def test_no_deadline_leaves_configured_timeouts_untouched(self, mock_config):
        mock_config.llm_tools_timeout_sec = 42.0
        mock_config.llm_embedding_timeout_sec = 17.0

        tool = ToolSearchTool()
        with patch(
            "jarvis.tools.builtin.tool_search.select_tools",
            return_value=["webSearch"],
        ) as mock_sel:
            tool.run({"query": "x"}, _ctx(mock_config))

        _, kwargs = mock_sel.call_args
        assert kwargs["llm_timeout_sec"] == 42.0
        assert kwargs["embed_timeout_sec"] == 17.0
