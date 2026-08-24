"""Engine-level integration: the tool router's per-turn classification
reaches the main chat call as ``chat_backend_preference``, without a
second LLM call, and the cached router entry carries it forward on a
hot-window replay.
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
    cfg.agentic_max_turns = 8
    cfg.tool_search_max_calls = 3
    cfg.tool_selection_strategy = "llm"
    cfg.tool_carryover_max_turns = 2
    cfg.tool_carryover_per_entry_chars = 1200
    cfg.mcps = {}
    cfg.llm_thinking_enabled = False
    cfg.tts_engine = "none"
    cfg.ollama_embed_model = "test-embed"
    cfg.db_path = ":memory:"
    return cfg


@pytest.mark.unit
@patch("src.jarvis.memory.graph_ops.format_warm_profile_block", return_value="")
@patch("src.jarvis.memory.graph_ops.build_warm_profile",
       return_value={"user": "", "directives": ""})
@patch("src.jarvis.memory.graph.GraphMemoryStore")
@patch("src.jarvis.reply.engine.plan_query", return_value=[])
@patch("src.jarvis.reply.engine.extract_search_params_for_memory", return_value={})
@patch("src.jarvis.reply.engine.extract_text_from_response")
@patch("src.jarvis.reply.engine.chat_with_messages")
@patch("src.jarvis.reply.engine.select_tools")
def test_router_classification_reaches_the_chat_call(
    mock_select_tools, mock_chat, mock_extract, _mock_extract_mem, _mock_plan,
    _mock_graph, _mock_warm, _mock_fmt,
):
    """select_tools's LLM call is the only classification call for this
    turn — its "complex" verdict must reach chat_with_messages, not a
    second LLM round-trip."""
    def _select_tools_side_effect(*args, **kwargs):
        signal = kwargs.get("chat_backend_signal")
        if signal is not None:
            signal["preference"] = "complex"
        return ["webSearch"]

    mock_select_tools.side_effect = _select_tools_side_effect
    mock_chat.return_value = {"message": {"content": "Here is a detailed plan."}}
    mock_extract.return_value = "Here is a detailed plan."

    db = Mock()
    cfg = _mock_cfg()
    dm = DialogueMemory()

    run_reply_engine(db=db, cfg=cfg, tts=None,
                     text="write me a detailed multi-step plan", dialogue_memory=dm)

    assert mock_chat.call_args_list, "chat_with_messages should have been called"
    assert mock_chat.call_args_list[-1].kwargs["chat_backend_preference"] == "complex"
    # Exactly one LLM classification call (select_tools) — chat_with_messages
    # itself is mocked and does no LLM work of its own here, so its call
    # count reflects only the reply turns, not a second classification hop.
    assert mock_select_tools.call_count == 1


@pytest.mark.unit
@patch("src.jarvis.memory.graph_ops.format_warm_profile_block", return_value="")
@patch("src.jarvis.memory.graph_ops.build_warm_profile",
       return_value={"user": "", "directives": ""})
@patch("src.jarvis.memory.graph.GraphMemoryStore")
@patch("src.jarvis.reply.engine.plan_query", return_value=[])
@patch("src.jarvis.reply.engine.extract_search_params_for_memory", return_value={})
@patch("src.jarvis.reply.engine.extract_text_from_response")
@patch("src.jarvis.reply.engine.chat_with_messages")
@patch("src.jarvis.reply.engine.select_tools")
def test_hot_cache_replay_carries_the_preference_forward(
    mock_select_tools, mock_chat, mock_extract, _mock_extract_mem, _mock_plan,
    _mock_graph, _mock_warm, _mock_fmt,
):
    """A cache hit must not silently drop the classification — the second
    identical query is served from cache and still reaches the chat call
    with the same preference, without calling select_tools again."""
    def _select_tools_side_effect(*args, **kwargs):
        signal = kwargs.get("chat_backend_signal")
        if signal is not None:
            signal["preference"] = "complex"
        return ["webSearch"]

    mock_select_tools.side_effect = _select_tools_side_effect
    mock_chat.return_value = {"message": {"content": "An answer."}}
    mock_extract.return_value = "An answer."

    db = Mock()
    cfg = _mock_cfg()
    dm = DialogueMemory()

    run_reply_engine(db=db, cfg=cfg, tts=None,
                     text="write me a detailed multi-step plan", dialogue_memory=dm)
    run_reply_engine(db=db, cfg=cfg, tts=None,
                     text="write me a detailed multi-step plan", dialogue_memory=dm)

    assert mock_select_tools.call_count == 1, "second identical turn should hit the hot-window cache"
    assert mock_chat.call_args_list[-1].kwargs["chat_backend_preference"] == "complex"
