"""Tests for conversation mode: listening on without a wake word per question.

Behaviour under test is what a user would notice: whether speech is answered
without addressing Jarvis by name, whether the mode survives the follow-up
window, and whether asking Jarvis to stop actually ends it.
"""

import time
from unittest.mock import patch

import pytest

from jarvis.listening.conversation_mode import (
    conversation_mode_active,
    register_conversation_controller,
    set_conversation_mode,
)
from jarvis.listening.state_manager import ListeningState, StateManager

from test_hot_window_input import (
    _accepted_query,
    _create_listener,
    _install_intent_judge,
    _make_judgment,
)


@pytest.fixture
def state_manager():
    manager = StateManager(hot_window_seconds=0.05, echo_tolerance=0.01)
    yield manager
    manager.stop()


@pytest.fixture(autouse=True)
def _clear_registered_controller():
    yield
    register_conversation_controller(None)


@pytest.mark.unit
class TestTheConversationStaysOpen:
    def test_speech_needs_no_wake_word_while_a_conversation_runs(self, state_manager) -> None:
        state_manager.start_conversation()

        long_ago = time.time() - 3600

        assert state_manager.was_speech_during_hot_window(long_ago, long_ago + 1) is True

    def test_the_conversation_outlives_the_follow_up_window(self, state_manager) -> None:
        """The window's timer must not quietly close a conversation."""
        state_manager.start_conversation()

        state_manager.expire_hot_window()
        time.sleep(0.1)

        assert state_manager.is_conversation_active is True
        assert state_manager.was_speech_during_hot_window(time.time()) is True

    def test_ending_the_conversation_returns_to_wake_word_listening(self, state_manager) -> None:
        state_manager.start_conversation()

        state_manager.end_conversation()

        assert state_manager.is_conversation_active is False
        assert state_manager.get_state() == ListeningState.WAKE_WORD
        assert state_manager.was_speech_during_hot_window(time.time()) is False

    def test_stopping_the_listener_closes_the_conversation(self, state_manager) -> None:
        state_manager.start_conversation()

        state_manager.stop()

        assert state_manager.is_conversation_active is False


@pytest.mark.unit
class TestTalkingWithoutTheWakeWord:
    @patch("builtins.print")
    def test_a_question_is_answered_without_being_addressed_by_name(self, _print) -> None:
        listener, _ = _create_listener()
        listener.state_manager.start_conversation()
        _install_intent_judge(
            listener, _make_judgment(directed=True, query="what time is it")
        )

        listener._process_transcript("what time is it", utterance_energy=0.01)

        assert _accepted_query(listener) == "what time is it"
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_asking_jarvis_to_stop_ends_the_conversation(self, _print) -> None:
        listener, _ = _create_listener()
        listener.state_manager.start_conversation()
        _install_intent_judge(listener, _make_judgment(directed=True, query="", stop=True))

        listener._process_transcript("jarvis stop", utterance_energy=0.01)

        assert listener.state_manager.is_conversation_active is False
        assert _accepted_query(listener) == ""
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_a_stop_outside_a_conversation_changes_nothing(self, _print) -> None:
        """Jarvis does not support spoken interruption, and that stays true."""
        listener, _ = _create_listener()
        listener.echo_detector.track_tts_start("Here is the weather.")
        _install_intent_judge(listener, _make_judgment(directed=True, query="", stop=True))

        listener._process_transcript("stop", utterance_energy=0.01)

        assert listener.state_manager.is_conversation_active is False
        assert listener.state_manager.get_state() == ListeningState.WAKE_WORD
        assert _accepted_query(listener) == ""
        listener.state_manager.stop()


@pytest.mark.unit
class TestTheControlCentreSwitch:
    def test_a_caller_outside_the_voice_loop_turns_the_conversation_on_and_off(
        self, state_manager
    ) -> None:
        register_conversation_controller(state_manager)

        assert set_conversation_mode(True) is True
        assert conversation_mode_active() is True

        assert set_conversation_mode(False) is True
        assert conversation_mode_active() is False

    def test_the_switch_reports_failure_when_nothing_is_listening(self) -> None:
        register_conversation_controller(None)

        assert set_conversation_mode(True) is False
        assert conversation_mode_active() is False


@pytest.mark.unit
class TestTheInterfaceCanSeeTheConversation:
    """A switch nobody can watch is a switch nobody can trust.

    The conversation also ends on its own, when the judge decides the user
    asked Jarvis to stop, so an interface that only knew what it had itself
    turned on would go stale the moment the user spoke.
    """

    def test_starting_a_conversation_is_published(self, state_manager) -> None:
        from jarvis.runtime import get_runtime_state

        state_manager.start_conversation()

        assert get_runtime_state().snapshot()["conversation"]["active"] is True

    def test_ending_a_conversation_is_published(self, state_manager) -> None:
        from jarvis.runtime import get_runtime_state

        state_manager.start_conversation()
        state_manager.end_conversation()

        assert get_runtime_state().snapshot()["conversation"]["active"] is False

    def test_a_watcher_hears_the_change(self, state_manager) -> None:
        from jarvis.runtime import get_event_bus

        with get_event_bus().subscribe() as subscription:
            state_manager.start_conversation()
            events = subscription.listen(timeout=1.0)
            event = next(events)

        assert event is not None
        assert event["kind"] == "conversation"
        assert event["data"]["active"] is True
