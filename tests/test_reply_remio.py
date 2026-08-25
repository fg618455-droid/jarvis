"""Reply-engine integration for bounded Remio memory enrichment."""

from __future__ import annotations

from unittest.mock import patch

from jarvis.memory.provenance import MemoryProvenance, RetrievedSnippet


def _reply(text: str = "ready") -> dict:
    return {"message": {"role": "assistant", "content": text}}


def test_remio_enrichment_is_enabled_by_default():
    from jarvis.config import get_default_config

    assert get_default_config()["remio_memory_enabled"] is True


def test_planned_memory_search_adds_attributable_remio_context(
    mock_config, db, dialogue_memory,
):
    from jarvis.reply import engine as engine_mod

    mock_config.remio_memory_enabled = True
    mock_config.memory_enrichment_source = "diary"
    mock_config.memory_digest_enabled = False
    mock_config.evaluator_enabled = False
    mock_config.memory_reply_first_audio_sec = 10.0
    captured_system: list[str] = []

    def fake_chat(*args, **kwargs):
        captured_system.append(kwargs["messages"][0]["content"])
        return _reply()

    with patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(
             engine_mod,
             "plan_query",
             return_value=["searchMemory topic='project alpha'", "Reply to the user."],
         ), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": ["project", "alpha"], "questions": []},
         ), \
         patch(
             "jarvis.memory.conversation.search_conversation_memory_by_keywords",
             return_value=[],
         ), \
         patch(
             "jarvis.memory.remio.RemioAdapter.search",
             return_value=[RetrievedSnippet(
                 "Alpha launches on Friday.",
                 MemoryProvenance.remio("Project Alpha"),
             )],
         ), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="what do my project alpha notes say",
            dialogue_memory=dialogue_memory,
        )

    assert result == "ready"
    assert captured_system
    assert "[Remio note excerpt]" in captured_system[0]
    assert "Project Alpha" not in captured_system[0]
    assert "Alpha launches on Friday." in captured_system[0]
    retained = dialogue_memory.hot_cache_get(
        dialogue_memory.MEMORY_PROVENANCE_CACHE_KEY,
    )
    assert retained[0].provenance == MemoryProvenance.remio("Project Alpha")


def test_reply_only_plan_does_not_start_remio(mock_config, db, dialogue_memory):
    from jarvis.reply import engine as engine_mod

    mock_config.remio_memory_enabled = True
    mock_config.evaluator_enabled = False
    dialogue_memory.hot_cache_put(
        dialogue_memory.MEMORY_PROVENANCE_CACHE_KEY,
        [RetrievedSnippet(
            "stale source",
            MemoryProvenance.remio("Previous note"),
        )],
    )

    with patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(engine_mod, "plan_query", return_value=["Reply to the user."]), \
         patch("jarvis.memory.remio.RemioAdapter.search") as search, \
         patch.object(engine_mod, "chat_with_messages", return_value=_reply()):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="hello there",
            dialogue_memory=dialogue_memory,
        )

    assert result == "ready"
    search.assert_not_called()
    assert dialogue_memory.hot_cache_get(
        dialogue_memory.MEMORY_PROVENANCE_CACHE_KEY,
    ) == []
