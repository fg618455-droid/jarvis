"""
Chat Backend Routing Classification Evaluations

Tests that the tool router's reused LLM call (jarvis.tools.selection
._select_llm) also classifies a turn as needing the local fast route, the
complex-reasoning route, or the crew_chat route, reusing the SAME response
the router already produces rather than a second LLM call. This demonstrates
the improvement behind the "auto" chat_backend_override across all three
legs jointly: a short conversational turn should route local, a turn
needing careful structured output or front-end-shaped reasoning should
route to the complex-reasoning backend, and a turn about backend code,
infrastructure, or systems work should route to the crew.

The LOCAL and HERMES legs discriminate reliably against a small local judge
(e.g. gemma4:e2b); COMPLEX vs HERMES is a genuinely close call for a model
that size on some queries, since both read as "needs real thinking" to it.
A flake on a COMPLEX case here reflects the configured judge's own capacity
for a subtle three-way split, not a bug in the extraction or routing
mechanism, which is deterministic and covered separately in
tests/test_tool_selection.py, tests/test_chat_backend_routing.py, and
tests/test_engine_chat_backend_routing.py.

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
            "Design a clean, accessible onboarding flow for a mobile app and "
            "write the SwiftUI code for the first screen, explaining your UX "
            "reasoning as you go."
        ),
        "complex",
        id="front-end design and code task routes complex",
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
    pytest.param(
        (
            "The overnight cron job on the home server keeps failing "
            "silently. Read through the server logs, find out why it is "
            "crashing, and fix the script so it retries safely."
        ),
        "hermes",
        id="server log investigation and fix routes hermes",
    ),
    pytest.param(
        (
            "Set up a CI pipeline that runs our backend test suite, builds a "
            "Docker image, and deploys it to the staging server on every "
            "merge to main."
        ),
        "hermes",
        id="infrastructure and deployment task routes hermes",
    ),
]


@pytest.mark.eval
class TestChatBackendRoutingClassification:
    """The router's single LLM call also names a coarse chat-backend
    preference; this must actually discriminate local, complex, and hermes
    turns from one another for the automatic "auto" override to be worth
    anything across all three legs, not just the newest one in isolation."""

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
