"""Tests for the Kokoro TTS engine.

Kokoro is Jarvis's second local, offline TTS engine alongside Piper. The
actual synthesis (the Kokoro half of backtalk's ``mouth.py``, vendored into
``jarvis.output.vendor.kokoro_backtalk``) runs in its own sidecar
subprocess, reached through
:class:`jarvis.output.kokoro_sidecar_client.KokoroSidecarClient` — see
``tests/test_kokoro_sidecar_client.py`` for that boundary's own tests.
:class:`KokoroTTS` itself follows the same shape as
:class:`jarvis.output.tts.PiperTTS` (queue, worker thread, interruptible
playback), so ``create_tts_engine`` can select between them the same way it
already selects Piper vs Chatterbox.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestKokoroTTSInterface:
    """KokoroTTS should offer the same interface as the other engines."""

    def test_has_required_methods(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=False)

        assert callable(tts.start)
        assert callable(tts.stop)
        assert callable(tts.speak)
        assert callable(tts.interrupt)
        assert callable(tts.is_speaking)
        assert callable(tts.get_last_spoken_text)

    def test_initialization_disabled(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=False)

        tts.start()
        tts.speak("test text")
        assert tts.is_speaking() is False
        tts.interrupt()
        tts.stop()

    def test_initialization_with_all_parameters(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(
            enabled=True,
            voice="test-voice",
            rate=200,
            kokoro_voice="bm_lewis",
            kokoro_speed=1.2,
        )

        assert tts.enabled is True
        assert tts.kokoro_voice == "bm_lewis"
        assert tts.kokoro_speed == 1.2

    def test_default_voice_and_speed(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=False)

        assert tts.kokoro_voice
        assert tts.kokoro_speed == 1.0


class TestKokoroTTSWithMocking:
    def test_speak_queues_text(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=True)
        tts.speak("Hello world")

        assert not tts._q.empty()

    def test_speak_does_nothing_when_disabled(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=False)
        tts.speak("Hello world")

        assert tts._q.empty()

    def test_speak_does_nothing_for_empty_text(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=True)
        tts.speak("")
        tts.speak("   ")

        assert tts._q.empty()

    def test_interrupt_sets_flag(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=True)

        assert not tts._should_interrupt.is_set()
        tts.interrupt()
        assert tts._should_interrupt.is_set()

    def test_is_speaking_returns_event_state(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=True)

        assert tts.is_speaking() is False
        tts._is_speaking.set()
        assert tts.is_speaking() is True
        tts._is_speaking.clear()
        assert tts.is_speaking() is False

    def test_get_last_spoken_text_returns_stored_text(self):
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=True)

        assert tts.get_last_spoken_text() == ""
        tts._last_spoken_text = "Hello world"
        assert tts.get_last_spoken_text() == "Hello world"

    def test_start_does_not_launch_the_sidecar(self):
        """The sidecar is launched lazily, only when speech is actually
        requested, mirroring computer_interaction_enabled/
        system_management_enabled: enabling a feature is not using it."""
        from src.jarvis.output.tts import KokoroTTS

        tts = KokoroTTS(enabled=True)
        tts._sidecar = MagicMock()

        tts.start()
        tts.stop()

        tts._sidecar.synthesize.assert_not_called()

    def test_speak_once_calls_the_sidecar_with_the_text_voice_and_speed(self):
        """The exact call, not just "it worked" - mirrors how
        tests/tools/builtin/test_system_manager.py asserts subprocess calls."""
        from src.jarvis.output.tts import KokoroTTS, Utterance

        tts = KokoroTTS(enabled=True, kokoro_voice="jf_alpha", kokoro_speed=1.3)
        tts._sidecar = MagicMock()
        tts._sidecar.synthesize.return_value = iter([])

        tts._speak_once(Utterance(text="konnichiwa"))

        tts._sidecar.synthesize.assert_called_once_with("konnichiwa", "jf_alpha", 1.3)

    def test_each_utterance_makes_exactly_one_sidecar_call(self):
        """Sentence-by-sentence streaming: one sidecar round trip per queued
        utterance, never batched into a single call for a whole reply."""
        from src.jarvis.output.tts import KokoroTTS, Utterance

        tts = KokoroTTS(enabled=True)
        tts._sidecar = MagicMock()
        tts._sidecar.synthesize.return_value = iter([])

        tts._speak_once(Utterance(text="First sentence."))
        tts._speak_once(Utterance(text="Second sentence."))

        assert tts._sidecar.synthesize.call_count == 2

    def test_a_sidecar_failure_is_reported_not_raised(self):
        """A sidecar crash or a missing kokoro install surfaces through the
        normal TTS failure path (debug_log + a printed warning), never as a
        raw exception out of speak()."""
        from src.jarvis.output.kokoro_sidecar_client import KokoroSidecarError
        from src.jarvis.output.tts import KokoroTTS, Utterance

        tts = KokoroTTS(enabled=True)
        tts._sidecar = MagicMock()

        def _raise(*args, **kwargs):
            raise KokoroSidecarError("kokoro not installed")
            yield  # pragma: no cover - makes this a generator function

        tts._sidecar.synthesize.side_effect = _raise

        with patch("builtins.print") as mock_print:
            tts._speak_once(Utterance(text="hello"))  # must not raise

        assert tts.is_speaking() is False
        assert any("kokoro not installed" in str(call) for call in mock_print.call_args_list)


class TestKokoroTTSFactory:
    """create_tts_engine should select Kokoro the same way it already
    selects Piper and Chatterbox."""

    def test_creates_kokoro_engine(self):
        from src.jarvis.output.tts import create_tts_engine, KokoroTTS

        tts = create_tts_engine(engine="kokoro", enabled=False)

        assert isinstance(tts, KokoroTTS)

    def test_creates_kokoro_engine_case_insensitive(self):
        from src.jarvis.output.tts import create_tts_engine, KokoroTTS

        assert isinstance(create_tts_engine(engine="KOKORO", enabled=False), KokoroTTS)
        assert isinstance(create_tts_engine(engine="Kokoro", enabled=False), KokoroTTS)

    def test_passes_kokoro_parameters(self):
        from src.jarvis.output.tts import create_tts_engine, KokoroTTS

        tts = create_tts_engine(
            engine="kokoro",
            enabled=True,
            kokoro_voice="af_bella",
            kokoro_speed=0.9,
        )

        assert isinstance(tts, KokoroTTS)
        assert tts.kokoro_voice == "af_bella"
        assert tts.kokoro_speed == 0.9

    def test_piper_selection_does_not_create_kokoro(self):
        """The engine switch actually reaches a different class, not just
        a different label on the same one."""
        from src.jarvis.output.tts import create_tts_engine, PiperTTS, KokoroTTS

        tts = create_tts_engine(engine="piper", enabled=False)

        assert isinstance(tts, PiperTTS)
        assert not isinstance(tts, KokoroTTS)

    def test_kokoro_selection_does_not_create_piper(self):
        from src.jarvis.output.tts import create_tts_engine, PiperTTS, KokoroTTS

        tts = create_tts_engine(engine="kokoro", enabled=False)

        assert isinstance(tts, KokoroTTS)
        assert not isinstance(tts, PiperTTS)
