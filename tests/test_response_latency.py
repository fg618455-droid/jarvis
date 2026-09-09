"""Behavioural guards for the voice response hot path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _listener(*, speaking: bool = False):
    from tests.test_hot_window_input import _create_listener

    listener, tts = _create_listener(tts_speaking=speaking)
    listener._dispatch_query = MagicMock()
    return listener, tts


def test_edge_wake_utterance_dispatches_without_a_second_silence_window():
    """A VAD-complete wake utterance is ready for the reply engine."""
    listener, _ = _listener()
    judge = MagicMock()
    judge.available = True
    judge.judge.side_effect = AssertionError(
        "an unambiguous edge wake word must not pay for intent classification"
    )
    listener._intent_judge = judge

    listener._process_transcript("jarvis what time is it", utterance_energy=0.01)

    assert listener._dispatch_query.call_args.args[0] == "what time is it"


def test_subject_position_wake_word_still_uses_contextual_judgement():
    """A wake name used inside the utterance can be its subject, not an address."""
    from jarvis.listening.intent_judge import IntentJudgment

    listener, _ = _listener()
    judge = MagicMock()
    judge.available = True
    judge.judge.return_value = IntentJudgment(
        directed=True,
        query="tell me about jarvis cocker",
        stop=False,
        confidence="high",
        reasoning="the assistant name is part of a person's name",
    )
    listener._intent_judge = judge

    listener._process_transcript(
        "tell me about jarvis cocker", utterance_energy=0.01
    )

    assert listener._dispatch_query.call_args.args[0] == "tell me about jarvis cocker"
    assert judge.judge.called


def test_microphone_callback_discards_audio_while_tts_is_speaking():
    """Playback is a closed listening interval, so no barge-in audio is queued."""
    listener, _ = _listener(speaking=True)
    audio = MagicMock()
    audio.copy.return_value = object()

    listener._on_audio(audio, 320, None, None)

    assert listener._audio_q.empty()


def test_tts_start_discards_audio_captured_before_playback():
    """Audio already buffered at playback start cannot become a delayed command."""
    listener, _ = _listener()
    listener._audio_q.put_nowait(object())
    listener._utterance_frames = [object()]
    listener.is_speech_active = True

    listener.track_tts_start("spoken reply")

    assert listener._audio_q.empty()
    assert listener._utterance_frames == []
    assert listener.is_speech_active is False


def test_disabled_planner_does_not_inject_speculative_long_term_memory(
    mock_config, db, dialogue_memory
):
    """Disabling the planner keeps speculative recall off the reply hot path."""
    from jarvis.reply import engine as engine_mod

    mock_config.planner_enabled = False
    mock_config.memory_digest_enabled = False
    captured_system = []

    def fake_chat(*args, **kwargs):
        messages = kwargs["messages"]
        captured_system.append(messages[0]["content"])
        return {"message": {"role": "assistant", "content": "ready"}}

    with patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": ["latency-sentinel"]},
         ), \
         patch(
             "jarvis.memory.conversation.search_conversation_memory_by_keywords",
             return_value=["LATENCY_SENTINEL_FROM_SPECULATIVE_RECALL"],
         ), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="give me a concise answer",
            dialogue_memory=dialogue_memory,
        )

    assert reply == "ready"
    assert captured_system
    assert "LATENCY_SENTINEL_FROM_SPECULATIVE_RECALL" not in captured_system[0]


def test_warmed_reply_prefix_matches_the_live_system_prompt(
    mock_config, db, dialogue_memory
):
    """Startup prefill covers the stable head of the first model request."""
    from jarvis.reply import engine as engine_mod

    mock_config.planner_enabled = False
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    warmed_messages = []

    class Backend:
        def chat(self, model, messages, **kwargs):
            warmed_messages.extend(messages)
            return {"message": {"role": "assistant", "content": "OK"}}

    with patch.object(engine_mod, "get_llm_backend", return_value=Backend()):
        assert engine_mod.warm_up_reply_prefix(
            mock_config,
            mock_config.llm_chat_model,
            timeout_sec=5.0,
        )

    live_system = []

    def fake_chat(*args, **kwargs):
        live_system.append(kwargs["messages"][0]["content"])
        return {"message": {"role": "assistant", "content": "ready"}}

    with patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat):
        engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="answer briefly",
            dialogue_memory=dialogue_memory,
        )

    assert warmed_messages[0]["role"] == "system"
    assert live_system[0].startswith(warmed_messages[0]["content"])
