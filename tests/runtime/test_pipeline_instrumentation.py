"""Behaviour tests for the timings the live voice path produces.

The unit tests next door prove the recorder measures what it is told to.
These prove the voice path actually tells it: that a spoken turn arrives in
the history with the stages it passed through, that the phase tracks what
the assistant is doing, and that an utterance thrown away is counted with
the reason it was thrown away for.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from jarvis.runtime.state import Phase, get_runtime_state
from jarvis.runtime.telemetry import get_recorder


@pytest.fixture(autouse=True)
def _clean_runtime():
    get_recorder().abandon()
    get_recorder().clear()
    get_recorder().use_journal(None)
    get_runtime_state().reset()
    yield
    get_recorder().abandon()
    get_recorder().clear()
    get_recorder().use_journal(None)
    get_runtime_state().reset()


def _create_listener(tts_enabled=True):
    """A VoiceListener with the heavy subsystems mocked out."""
    cfg = MagicMock()
    cfg.whisper_model = "small"
    cfg.whisper_device = "auto"
    cfg.whisper_compute_type = "int8"
    cfg.whisper_backend = "faster-whisper"
    cfg.sample_rate = 16000
    cfg.vad_enabled = False
    cfg.vad_aggressiveness = 2
    cfg.echo_tolerance = 0.3
    cfg.echo_energy_threshold = 2.0
    cfg.hot_window_seconds = 3.0
    cfg.hot_window_enabled = True
    cfg.voice_device = None
    cfg.voice_debug = False
    cfg.tune_enabled = False
    cfg.wake_word = "jarvis"
    cfg.wake_aliases = []
    cfg.wake_fuzzy_ratio = 0.78
    cfg.stop_commands = ["stop"]
    cfg.tts_rate = 200
    cfg.transcript_buffer_duration_sec = 120.0
    cfg.fast_model = "qwen2.5:7b"
    cfg.intent_judge_timeout_sec = 3.0

    tts = MagicMock()
    tts.enabled = tts_enabled
    tts.is_speaking.return_value = False

    with patch("jarvis.listening.listener.webrtcvad", None), \
         patch("jarvis.listening.listener.sd", None), \
         patch("jarvis.listening.listener.np", None), \
         patch("jarvis.listening.listener.create_intent_judge", return_value=None):
        from jarvis.listening.listener import VoiceListener
        listener = VoiceListener(MagicMock(), cfg, tts, MagicMock())

    return listener, tts


class TestASpokenTurn:
    def test_a_reply_produces_one_finished_turn(self):
        listener, tts = _create_listener()

        with patch("jarvis.reply.engine.run_reply_engine", return_value="Es ist drei Uhr."):
            listener._dispatch_query("wie spät ist es")
        # The turn closes when sound starts, which the engine reports.
        tts.speak.call_args.kwargs["audio_start_callback"]()

        history = get_recorder().history()
        assert len(history) == 1
        assert history[0]["transcript"] == "wie spät ist es"
        assert history[0]["reply"] == "Es ist drei Uhr."
        assert history[0]["total_ms"] > 0

    def test_the_turn_is_open_until_sound_starts(self):
        """Synthesis is part of the wait, so the clock runs through it."""
        listener, tts = _create_listener()

        with patch("jarvis.reply.engine.run_reply_engine", return_value="Ja."):
            listener._dispatch_query("bist du da")

        assert get_recorder().history() == []

        tts.speak.call_args.kwargs["audio_start_callback"]()
        assert len(get_recorder().history()) == 1

    def test_speech_synthesis_is_one_of_the_measured_stages(self):
        listener, tts = _create_listener()

        with patch("jarvis.reply.engine.run_reply_engine", return_value="Ja."):
            listener._dispatch_query("bist du da")
        tts.speak.call_args.kwargs["audio_start_callback"]()

        stages = [s["name"] for s in get_recorder().history()[0]["stages"]]
        assert "tts_synth" in stages

    def test_a_streamed_turn_is_recorded_once_the_reply_text_is_known(self):
        """Streaming makes sound start before the text is finished.

        The turn still needs both — the reply it produced and the moment the
        wait ended — so it is written when the second of the two arrives,
        whichever that is, and only once.
        """
        listener, tts = _create_listener()

        def stream_one_sentence(*args, on_speech_segment=None, **kwargs):
            on_speech_segment("Ja.")
            return "Ja."

        with patch("jarvis.reply.engine.run_reply_engine",
                   side_effect=stream_one_sentence):
            listener._dispatch_query("bist du da")
            tts.speak.call_args.kwargs["audio_start_callback"]()

        history = get_recorder().history()
        assert len(history) == 1
        assert history[0]["reply"] == "Ja."
        assert "tts_synth" in [s["name"] for s in history[0]["stages"]]

    def test_a_reply_without_speech_still_finishes_the_turn(self):
        listener, _ = _create_listener(tts_enabled=False)

        with patch("jarvis.reply.engine.run_reply_engine", return_value="Ja."):
            listener._dispatch_query("bist du da")

        assert len(get_recorder().history()) == 1

    def test_a_failing_reply_engine_files_the_turn_with_its_error(self):
        listener, _ = _create_listener()

        with patch("jarvis.reply.engine.run_reply_engine", side_effect=RuntimeError("model gone")):
            listener._dispatch_query("wie spät ist es")

        history = get_recorder().history()
        assert history[0]["error"] == "model gone"
        assert get_runtime_state().snapshot()["errors"] == 1


class TestPhase:
    def test_thinking_while_the_reply_is_being_made(self):
        listener, _ = _create_listener()
        seen = []

        def _watch(*args, **kwargs):
            seen.append(get_runtime_state().snapshot()["phase"])
            return "Ja."

        with patch("jarvis.reply.engine.run_reply_engine", side_effect=_watch):
            listener._dispatch_query("bist du da")

        assert seen == ["thinking"]

    def test_speaking_once_sound_starts(self):
        listener, tts = _create_listener()

        with patch("jarvis.reply.engine.run_reply_engine", return_value="Ja."):
            listener._dispatch_query("bist du da")
        tts.speak.call_args.kwargs["audio_start_callback"]()

        assert get_runtime_state().snapshot()["phase"] == "speaking"

    def test_waiting_again_when_there_was_nothing_to_say(self):
        listener, _ = _create_listener(tts_enabled=False)

        with patch("jarvis.reply.engine.run_reply_engine", return_value="Ja."):
            listener._dispatch_query("bist du da")

        assert get_runtime_state().snapshot()["phase"] == "idle"


class TestDiscardedUtterances:
    """The counts that answer "why did it ignore me"."""

    def test_a_turn_that_never_reached_the_reply_engine_leaves_no_trace(self):
        listener, _ = _create_listener()
        listener._dispatch_query = MagicMock()

        listener._process_transcript("irgendein Hintergrundgeplapper", 0.01)

        assert get_recorder().history() == []
        assert get_recorder().current() is None

    def test_speech_the_recogniser_produced_nothing_from_is_counted(self):
        state = get_runtime_state()

        state.count_discard("no_speech")

        assert state.snapshot()["discarded"]["no_speech"] == 1
