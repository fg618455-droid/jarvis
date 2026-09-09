"""Recovery from malformed small-model output in the reply loop."""

from __future__ import annotations

from unittest.mock import patch


def _assistant(text: str) -> dict:
    return {"message": {"role": "assistant", "content": text}}


def test_malformed_first_answer_is_retried_before_user_fallback(
    mock_config, db, dialogue_memory,
):
    """A recoverable protocol leak must not become the spoken reply."""
    from jarvis.reply import engine as engine_mod

    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.evaluator_enabled = False
    captured_messages: list[list[dict]] = []

    responses = iter([
        _assistant("Ich sehe nach.\ntool_calls: []"),
        _assistant(
            "In meiner gespeicherten Erinnerung finde ich gerade keine "
            "verlässlichen Angaben über dich."
        ),
    ])

    def fake_chat(*args, **kwargs):
        captured_messages.append([dict(message) for message in kwargs["messages"]])
        return next(responses)

    with patch.object(engine_mod, "select_tools", return_value=[]), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="Kannst du mir sagen, wer ich bin?",
            dialogue_memory=dialogue_memory,
        )

    assert reply.startswith("In meiner gespeicherten Erinnerung")
    assert len(captured_messages) == 2
    assert any(
        "previous response was invalid" in str(message.get("content", ""))
        for message in captured_messages[1]
    )
    assert "Setup Wizard" not in reply


def test_qwen_small_model_uses_native_tools_before_text_fallback(
    mock_config, db, dialogue_memory,
):
    """Qwen's declared tool capability must not be hidden by its size."""
    from jarvis.reply import engine as engine_mod

    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.evaluator_enabled = False
    calls: list[dict] = []

    def fake_chat(*args, **kwargs):
        calls.append(kwargs)
        return _assistant("Eine normale Antwort.")

    with patch.object(engine_mod, "select_tools", return_value=[]), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="Hallo",
            dialogue_memory=dialogue_memory,
        )

    assert reply == "Eine normale Antwort."
    assert calls[0]["tools"], "Qwen must receive the native tool schema"
    assert "Exact tool-call syntax" not in calls[0]["messages"][0]["content"]
