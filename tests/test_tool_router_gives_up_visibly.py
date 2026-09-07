"""A router that gives up on every turn must look like a fault.

The LLM router falling back is fail-open by design and each fallback is
harmless on its own. What is not harmless is the router failing on
*every* turn: the chat model then sees the whole catalogue each time, the
zero-tool grounding gate stops activating because a full catalogue reads
as a fallback shape rather than a relevance signal, and small models
drown. That state ran for a whole evening without anything saying so.

Measured cause: a reasoning model spent the router's entire 50-token cap
thinking, both FAST routes ran that family, and the chain came back
empty. The recovery for that lives in the backend. What lives here is the
part that matters when the next cause is different: saying it out loud.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis.tools.selection import ToolSelectionStrategy, select_tools


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.inputSchema = {"type": "object", "properties": {}}


def _catalogue():
    return {
        "webSearch": _Tool("webSearch", "Search the web for current information."),
        "getWeather": _Tool("getWeather", "Report the weather for a place."),
        "stop": _Tool("stop", "End the conversation."),
        "toolSearchTool": _Tool("toolSearchTool", "Find more tools."),
    }


def _router(answer):
    backend = MagicMock()
    backend.direct.return_value = answer
    return backend


class TestGivingUpIsSaidOutLoud:
    def test_an_empty_router_response_is_reported(self, capsys):
        select_tools(
            "what is the weather in London",
            _catalogue(),
            {},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=_router(""),
            llm_model="fast-model",
        )

        printed = capsys.readouterr().out
        assert "router" in printed.lower()

    def test_a_router_that_named_nothing_known_is_reported(self, capsys):
        select_tools(
            "what is the weather in London",
            _catalogue(),
            {},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=_router("I think you should look outside"),
            llm_model="fast-model",
        )

        printed = capsys.readouterr().out
        assert "router" in printed.lower()

    def test_a_router_that_failed_outright_is_reported(self, capsys):
        backend = MagicMock()
        backend.direct.side_effect = RuntimeError("boom")

        select_tools(
            "what is the weather in London",
            _catalogue(),
            {},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=backend,
            llm_model="fast-model",
        )

        printed = capsys.readouterr().out
        assert "router" in printed.lower()


class TestAWorkingRouterStaysQuiet:
    def test_a_narrowed_selection_says_nothing(self, capsys):
        selected = select_tools(
            "what is the weather in London",
            _catalogue(),
            {},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=_router("getWeather LOCAL"),
            llm_model="fast-model",
        )

        assert "getWeather" in selected
        assert capsys.readouterr().out.strip() == ""

    def test_a_positive_no_tool_decision_says_nothing(self, capsys):
        """`none` is the router working, not the router giving up."""
        select_tools(
            "hello there",
            _catalogue(),
            {},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=_router("none LOCAL"),
            llm_model="fast-model",
        )

        assert capsys.readouterr().out.strip() == ""
