"""Voice interaction after a standalone wake word."""

from __future__ import annotations

from unittest.mock import patch

from test_hot_window_input import _accepted_query, _create_listener, _install_intent_judge, _make_judgment


@patch("builtins.print")
def test_a_bare_wake_word_confirms_readiness_and_collects_one_request(_print):
    listener, tts = _create_listener()
    listener.cfg.wake_acknowledgement = "Ready for your request."

    listener._process_transcript("Jarvis", utterance_energy=0.01)

    assert _accepted_query(listener) == ""
    assert listener.state_manager.is_command_capture_active is True
    tts.speak.assert_called_once_with(
        "Ready for your request.",
        completion_callback=listener.echo_detector.track_tts_finish,
    )

    _install_intent_judge(listener, _make_judgment(directed=True, query="what time is it"))
    listener._process_transcript("what time is it", utterance_energy=0.01)

    assert _accepted_query(listener) == "what time is it"
    assert listener.state_manager.is_command_capture_active is False
    assert listener.state_manager.is_conversation_active is False
    listener.state_manager.stop()


@patch("builtins.print")
def test_conversation_request_after_wake_acknowledgement_stays_open(_print):
    listener, _ = _create_listener()
    listener.cfg.wake_acknowledgement = "Ready for your request."
    listener.cfg.conversation_mode_acknowledgement = "Conversation stays open."
    listener._process_transcript("Jarvis", utterance_energy=0.01)
    _install_intent_judge(
        listener,
        _make_judgment(directed=True, query="", conversation_mode=True),
    )

    listener._process_transcript("conversation mode", utterance_energy=0.01)

    assert listener.state_manager.is_conversation_active is True
    assert _accepted_query(listener) == ""
    listener.state_manager.stop()
