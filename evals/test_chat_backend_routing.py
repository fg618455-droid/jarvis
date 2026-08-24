"""
Chat Backend Routing Classification Evaluations

Tests that the tool router's reused LLM call (jarvis.tools.selection
._select_llm) also classifies a turn as needing the local fast route or
the complex-reasoning route, reusing the SAME response the router already
produces rather than a second LLM call. This demonstrates the improvement
behind the "auto" chat_backend_override: a short conversational turn
should route local, and a turn that clearly needs multi-step reasoning,
code, or careful structured output should route to the complex-reasoning
backend.

Run: .venv/bin/python -m pytest evals/test_chat_backend_routing.py -v
"""

import pytest

from conftest import requires_judge_llm
from helpers import JUDGE_MODEL


CHAT_BACKEND_ROUTING_CASES = [
    pytest.param(
        "hey, how's it going?",
        "local",
        id="pure small talk routes local",
    ),
    pytest.param(
        "what's the weather like tomorrow",
        "local",
        id="short single-fact lookup routes local",
    ),
    pytest.param(
        (
            "Write me a Python script that scrapes a list of product prices "
            "from a webpage, stores them in a SQLite database with a schema "
            "you design, and then explain the tradeoffs of that schema versus "
            "a normalised multi-table design."
        ),
        "complex",
        id="multi-step coding and design tradeoff task routes complex",
    ),
    pytest.param(
        (
            "Compare three different approaches to caching in a distributed "
            "system in depth, weighing consistency, latency, and failure "
            "modes for each, and lay out your reasoning step by step."
        ),
        "complex",
        id="in-depth multi-step comparison routes complex",
    ),
]


@pytest.mark.eval
class TestChatBackendRoutingClassification:
    """The router's single LLM call also names a coarse chat-backend
    preference; this must actually discriminate simple from complex turns
    for the automatic "auto" override to be worth anything."""

    @requires_judge_llm
    @pytest.mark.parametrize("query, expected_preference", CHAT_BACKEND_ROUTING_CASES)
    def test_router_classifies_turn_complexity(
        self,
        mock_config,
        query,
        expected_preference,
    ):
        from jarvis.llm import get_llm_backend
        from jarvis.tools.selection import select_tools, ToolSelectionStrategy
        from jarvis.tools.registry import BUILTIN_TOOLS

        signal: dict = {}
        select_tools(
            query=query,
            builtin_tools=BUILTIN_TOOLS,
            mcp_tools={},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=get_llm_backend(mock_config),
            llm_model=JUDGE_MODEL,
            llm_timeout_sec=15.0,
            chat_backend_signal=signal,
        )

        preference = signal.get("preference")
        assert preference is not None, (
            f"[{JUDGE_MODEL}] router response carried no LOCAL/COMPLEX "
            "classification at all"
        )
        assert preference == expected_preference, (
            f"[{JUDGE_MODEL}] expected {expected_preference!r} for {query!r}, "
            f"got {preference!r}"
        )

        print(f"  ✅ [{JUDGE_MODEL}] {query!r} -> {preference}")
