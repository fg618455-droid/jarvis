"""An exhausted route chain is a routing fault, not a confused model.

When no configured backend answers, the reply engine used to deliver the
same "I had trouble processing that" apology it uses when a model produces
something unusable. The two failures need different words: one asks the
user to rephrase, which cannot possibly help, while the other tells them
the assistant could not reach a model at all. Reading the routing fault as
a comprehension failure hides it from the user and from the logs.
"""

from unittest.mock import Mock, patch

import pytest

from src.jarvis.memory.conversation import DialogueMemory
from src.jarvis.reply.engine import run_reply_engine


def _mock_cfg():
    cfg = Mock()
    cfg.ollama_base_url = "http://localhost:11434"
    cfg.ollama_chat_model = "test-large"
    cfg.llm_chat_model = "test-large"
    cfg.chat_backend_override = "auto"
    cfg.voice_debug = False
    cfg.llm_tools_timeout_sec = 8.0
    cfg.llm_embedding_timeout_sec = 10.0
    cfg.llm_chat_timeout_sec = 45.0
    cfg.llm_digest_timeout_sec = 8.0
    cfg.memory_enrichment_max_results = 5
    cfg.memory_enrichment_source = "diary"
    cfg.memory_digest_enabled = False
    cfg.tool_result_digest_enabled = False
    cfg.location_ip_address = None
    cfg.location_auto_detect = False
    cfg.location_enabled = False
    cfg.agentic_max_turns = 3
    cfg.tool_search_max_calls = 3
    cfg.tool_selection_strategy = "llm"
    cfg.tool_carryover_max_turns = 2
    cfg.tool_carryover_per_entry_chars = 1200
    cfg.mcps = {}
    cfg.llm_thinking_enabled = False
    cfg.tts_engine = "none"
    cfg.ollama_embed_model = "test-embed"
    cfg.db_path = ":memory:"
    cfg.planner_enabled = False
    return cfg


def _run(text, selected_tools, chat_return):
    """Run one reply with the chat call answering ``chat_return``."""
    with patch("src.jarvis.memory.graph_ops.format_warm_profile_block", return_value=""), \
         patch("src.jarvis.memory.graph_ops.build_warm_profile",
               return_value={"user": "", "directives": ""}), \
         patch("src.jarvis.memory.graph.GraphMemoryStore"), \
         patch("src.jarvis.reply.engine.plan_query", return_value=[]), \
         patch("src.jarvis.reply.engine.extract_search_params_for_memory", return_value={}), \
         patch("src.jarvis.reply.engine.digest_loop_for_max_turns", return_value=None), \
         patch("src.jarvis.reply.engine.select_tools", return_value=selected_tools), \
         patch("src.jarvis.reply.engine.chat_with_messages",
               return_value=chat_return) as chat:
        reply = run_reply_engine(
            db=Mock(), cfg=_mock_cfg(), tts=None, text=text,
            dialogue_memory=DialogueMemory(),
        )
    return reply, chat


@pytest.mark.unit
def test_an_exhausted_chain_says_no_backend_answered():
    """Every configured route failed or is in cooldown. The user is told
    that, not asked to repeat themselves."""
    reply, chat = _run("what is in my calendar", ["stop"], None)

    assert chat.call_count >= 1
    assert "try again" not in reply.lower()
    assert "backend" in reply.lower() or "model" in reply.lower()


@pytest.mark.unit
def test_a_tool_turn_with_no_tool_capable_backend_names_that():
    """The chain still had routes, but none of them can carry a tool
    schema, so the requested action could not even be attempted. Saying
    "try again" invites the user to repeat a request that will fail the
    same way every time."""
    reply, _chat = _run(
        "put an appointment in my calendar", ["composio__CREATE_EVENT", "stop"], None,
    )

    assert "try again" not in reply.lower()
    assert "tool" in reply.lower()


@pytest.mark.unit
def test_a_model_that_answers_unusably_still_gets_the_ordinary_apology():
    """The routing message must not swallow the case it was split from: a
    backend that answered with nothing usable is a different failure and
    keeps its own words."""
    reply, _chat = _run(
        "hello there", ["stop"], {"message": {"role": "assistant", "content": ""}},
    )

    assert "try again" in reply.lower()
