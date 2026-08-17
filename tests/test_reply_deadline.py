"""Reply latency budgets without language-specific intent shortcuts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _reply(text: str = "ready") -> dict:
    return {"message": {"role": "assistant", "content": text}}


def test_reply_latency_defaults_are_safe_and_language_neutral():
    from jarvis.config import get_default_config

    defaults = get_default_config()
    assert defaults["simple_reply_first_audio_sec"] == 3.0
    assert defaults["memory_reply_first_audio_sec"] == 10.0
    assert defaults["memory_lookup_acknowledgement"] == ""
    assert "simple_reply_fast_path_enabled" not in defaults


def test_invalid_reply_budgets_fall_back_during_config_load(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "_config_version": 5,
        "simple_reply_first_audio_sec": "not-a-number",
        "memory_reply_first_audio_sec": None,
    }), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    from jarvis.config import load_settings

    settings = load_settings()
    assert settings.simple_reply_first_audio_sec == 3.0
    assert settings.memory_reply_first_audio_sec == 10.0


def test_planner_directed_memory_rebases_budget_and_notifies_once(
    mock_config, db, dialogue_memory,
):
    from jarvis.reply import engine as engine_mod

    mock_config.simple_reply_first_audio_sec = 3.0
    mock_config.memory_reply_first_audio_sec = 10.0
    mock_config.memory_digest_enabled = False
    mock_config.evaluator_enabled = False
    callback = MagicMock()
    budgets: list[float] = []
    real_after = engine_mod.RequestDeadline.after

    def record_budget(seconds, **kwargs):
        budgets.append(seconds)
        return real_after(seconds, **kwargs)

    with patch.object(engine_mod.RequestDeadline, "after", side_effect=record_budget), \
         patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(
             engine_mod,
             "plan_query",
             return_value=["searchMemory topic='project notes'", "Reply to the user."],
         ), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": [], "questions": []},
         ), \
         patch.object(engine_mod, "chat_with_messages", return_value=_reply()):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="consult my project notes",
            dialogue_memory=dialogue_memory,
            on_memory_lookup_started=callback,
        )

    assert result == "ready"
    assert budgets == [3.0, 10.0]
    callback.assert_called_once_with()


def test_reply_only_plan_does_not_claim_to_search_memory(
    mock_config, db, dialogue_memory,
):
    from jarvis.reply import engine as engine_mod

    mock_config.simple_reply_first_audio_sec = 3.0
    mock_config.memory_reply_first_audio_sec = 10.0
    mock_config.evaluator_enabled = False
    callback = MagicMock()

    with patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(engine_mod, "plan_query", return_value=["Reply to the user."]), \
         patch.object(engine_mod, "chat_with_messages", return_value=_reply()):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="hello there",
            dialogue_memory=dialogue_memory,
            on_memory_lookup_started=callback,
        )

    assert result == "ready"
    callback.assert_not_called()
