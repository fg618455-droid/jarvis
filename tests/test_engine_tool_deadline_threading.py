"""The reply turn's deadline must reach tool execution, not stop at the
top-level reply loop.

Tools that make their own blocking LLM/network call (getWeather's place
extractor, toolSearchTool's router re-run) read ``ToolContext.deadline``
to bound that call to what's actually left of the turn, instead of their
own configured ceiling (``llm_tools_timeout_sec``, 300s by default). That
only works if ``run_reply_engine`` actually passes its ``deadline`` down
through ``run_tool_with_retries`` at every tool-execution call site.
"""

from unittest.mock import Mock, patch

import pytest

from src.jarvis.llm.route import RequestDeadline
from src.jarvis.memory.conversation import DialogueMemory
from src.jarvis.reply.engine import run_reply_engine


def _mock_cfg():
    cfg = Mock()
    cfg.ollama_base_url = "http://localhost:11434"
    cfg.ollama_chat_model = "test-large"
    cfg.llm_chat_model = "test-large"
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
    cfg.tool_selection_strategy = "all"
    cfg.tool_carryover_max_turns = 2
    cfg.tool_carryover_per_entry_chars = 1200
    cfg.mcps = {}
    cfg.llm_thinking_enabled = False
    cfg.tts_engine = "none"
    cfg.ollama_embed_model = "test-embed"
    cfg.crew_handoff_enabled = False
    return cfg


@pytest.mark.unit
@patch("src.jarvis.reply.engine.plan_query", return_value=[])
@patch("src.jarvis.reply.engine.extract_search_params_for_memory", return_value={})
@patch("src.jarvis.reply.engine.run_tool_with_retries")
@patch("src.jarvis.reply.engine.extract_text_from_response")
@patch("src.jarvis.reply.engine.chat_with_messages")
def test_tool_execution_receives_the_turns_deadline(
    mock_chat, mock_extract, mock_tool, _mock_extract, _mock_plan,
):
    mock_tool.return_value = Mock(
        reply_text="It's 12 degrees in Berlin.",
        error_message=None,
    )
    mock_chat.side_effect = [
        {"message": {"content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "getWeather", "arguments": {"location": "Berlin"}},
        }]}},
        {"message": {"content": "It's 12 degrees in Berlin."}},
    ]
    mock_extract.side_effect = ["", "It's 12 degrees in Berlin."]

    db = Mock()
    cfg = _mock_cfg()
    dm = DialogueMemory()

    run_reply_engine(db=db, cfg=cfg, tts=None,
                     text="what's the weather in Berlin",
                     dialogue_memory=dm)

    mock_tool.assert_called_once()
    passed_deadline = mock_tool.call_args.kwargs.get("deadline")
    assert passed_deadline is not None, (
        "run_tool_with_retries must receive the turn's deadline so tools "
        "with their own blocking LLM/network calls can bound them"
    )
    assert isinstance(passed_deadline, RequestDeadline)
