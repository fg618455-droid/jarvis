"""Behaviour tests for pinning Whisper to a spoken language.

Automatic language identification is the right default, but it costs a
detection pass and lets Whisper wander into another language when room noise
is all it has to go on. A user who only ever speaks one language can name it,
and Whisper still handles loanwords from other languages inside that setting.

The setting holds no language of its own: it is whatever ISO-639-1 code the
user writes, and unset means automatic detection exactly as before.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from jarvis.config import load_settings, resolve_transcription_language


def _load_with(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


class TestResolveTranscriptionLanguage:
    def test_unset_means_automatic_detection(self, tmp_path, monkeypatch):
        """Existing setups keep detecting the language on upgrade."""
        cfg = _load_with(tmp_path, monkeypatch, {})

        assert resolve_transcription_language(cfg) is None

    @pytest.mark.parametrize("code", ["de", "nl", "tr", "ja"])
    def test_configured_code_is_handed_to_whisper(self, tmp_path, monkeypatch, code):
        cfg = _load_with(tmp_path, monkeypatch, {"whisper_language": code})

        assert resolve_transcription_language(cfg) == code

    @pytest.mark.parametrize("written", ["DE", " de ", "De\n"])
    def test_casing_and_stray_whitespace_do_not_break_it(self, tmp_path, monkeypatch, written):
        """Whisper only accepts lowercase codes, so a plausible spelling still works."""
        cfg = _load_with(tmp_path, monkeypatch, {"whisper_language": written})

        assert resolve_transcription_language(cfg) == "de"

    @pytest.mark.parametrize("written", ["", "   "])
    def test_blank_value_falls_back_to_detection(self, tmp_path, monkeypatch, written):
        cfg = _load_with(tmp_path, monkeypatch, {"whisper_language": written})

        assert resolve_transcription_language(cfg) is None

    @pytest.mark.parametrize("value", [None, 42, ["de"]])
    def test_unusable_value_falls_back_to_detection(self, value):
        """Fail open: a setting we cannot read must not silence the microphone."""
        cfg = MagicMock()
        cfg.whisper_language = value

        assert resolve_transcription_language(cfg) is None


def _listener_for_transcribe(whisper_language):
    """A VoiceListener wired up far enough to reach transcription."""
    import numpy as np

    mock_model = MagicMock()
    segment = MagicMock()
    segment.text = "guten morgen"
    info = MagicMock()
    info.language = "de"
    info.language_probability = 1.0
    mock_model.transcribe.return_value = (iter([segment]), info)

    with patch("jarvis.listening.listener.FASTER_WHISPER_AVAILABLE", True):
        with patch("jarvis.listening.listener.MLX_WHISPER_AVAILABLE", False):
            with patch("jarvis.listening.listener.WhisperModel"):
                from jarvis.listening.listener import VoiceListener

                cfg = MagicMock()
                cfg.sample_rate = 16000
                cfg.vad_enabled = False
                cfg.echo_tolerance = 0.3
                cfg.echo_energy_threshold = 2.0
                cfg.hot_window_seconds = 3.0
                cfg.voice_collect_seconds = 2.0
                cfg.voice_max_collect_seconds = 60.0
                cfg.tune_enabled = False
                cfg.voice_debug = False
                cfg.whisper_min_confidence = 0.3
                cfg.whisper_min_audio_duration = 0.15
                cfg.whisper_min_language_probability = 0.0
                cfg.whisper_language = whisper_language

                listener = VoiceListener(MagicMock(), cfg, MagicMock(), MagicMock())
                listener.model = mock_model
                listener._whisper_backend = "faster-whisper"
                listener._whisper_device = "cuda"
                listener._samplerate = 16000

                listener._utterance_frames = [np.zeros(16000, dtype=np.float32)]
                listener.echo_detector._utterance_start_time = time.time() - 1.0
                listener.is_speech_active = True

                return listener, mock_model


class TestListenerHonoursTheSetting:
    def test_configured_language_reaches_whisper(self):
        listener, model = _listener_for_transcribe("de")

        listener._finalize_utterance()

        assert model.transcribe.call_args[1]["language"] == "de"

    def test_unset_language_leaves_detection_on(self):
        listener, model = _listener_for_transcribe("")

        listener._finalize_utterance()

        assert model.transcribe.call_args[1]["language"] is None


class TestDictationHonoursTheSetting:
    def _engine(self, whisper_language):
        from jarvis.dictation.dictation_engine import DictationEngine

        cfg = MagicMock()
        cfg.whisper_language = whisper_language
        return DictationEngine(
            whisper_model_ref=lambda: None,
            whisper_backend_ref=lambda: "faster-whisper",
            mlx_repo_ref=lambda: None,
            transcribe_lock=threading.Lock(),
            cfg=cfg,
        )

    def test_configured_language_reaches_whisper(self):
        engine = self._engine("de")
        model = MagicMock()
        segment = MagicMock()
        segment.text = "guten morgen"
        model.transcribe.return_value = (iter([segment]), MagicMock())

        engine._transcribe_faster_whisper(model, object())

        assert model.transcribe.call_args[1]["language"] == "de"

    def test_unset_language_leaves_detection_on(self):
        engine = self._engine("")
        model = MagicMock()
        model.transcribe.return_value = (iter([]), MagicMock())

        engine._transcribe_faster_whisper(model, object())

        assert model.transcribe.call_args[1]["language"] is None


class TestEveryTranscriptionPathHonoursTheSetting:
    """One call site that forgets the language undoes the setting entirely.

    A user who pinned German and hears Icelandic come back has no way to
    tell which of the several `transcribe()` calls dropped it, so each is
    held to the same rule here: the configured language reaches Whisper,
    and no fallback quietly restores identification.
    """

    def test_the_older_api_fallback_keeps_the_language(self):
        """A faster-whisper build without `hotwords` or `vad_filter` raises
        TypeError, and the retry must not drop the language along with the
        arguments it was retrying without."""
        listener, model = _listener_for_transcribe("de")
        segment = MagicMock()
        segment.text = "guten morgen"
        info = MagicMock()
        info.language = "de"
        info.language_probability = 1.0
        calls = []

        def only_the_simple_signature(audio, **kwargs):
            calls.append(kwargs)
            if set(kwargs) - {"language"}:
                raise TypeError("unexpected keyword argument")
            return (iter([segment]), info)

        model.transcribe.side_effect = only_the_simple_signature

        listener._finalize_utterance()

        assert len(calls) == 2, "the rich call should have been retried"
        assert calls[-1]["language"] == "de"

    def test_the_security_confirmation_capture_keeps_the_language(self):
        """A spoken yes or no is transcribed on its own path. Detecting the
        language there instead of pinning it risks misreading the one word
        that decides whether a critical tool runs."""
        listener, model = _listener_for_transcribe("de")
        segment = MagicMock()
        segment.text = "ja"
        model.transcribe.return_value = (iter([segment]), MagicMock())

        listener._transcribe_security_audio(object())

        assert model.transcribe.call_args[1]["language"] == "de"

    def test_the_security_captures_older_api_fallback_keeps_the_language(self):
        listener, model = _listener_for_transcribe("de")
        segment = MagicMock()
        segment.text = "ja"
        calls = []

        def only_the_simple_signature(audio, **kwargs):
            calls.append(kwargs)
            if set(kwargs) - {"language"}:
                raise TypeError("unexpected keyword argument")
            return (iter([segment]), MagicMock())

        model.transcribe.side_effect = only_the_simple_signature

        listener._transcribe_security_audio(object())

        assert len(calls) == 2
        assert calls[-1]["language"] == "de"

    def test_the_mlx_security_capture_keeps_the_language(self):
        """The Apple Silicon branch is a separate call with its own
        argument list, so it needs its own guard."""
        from unittest.mock import patch as _patch

        listener, _model = _listener_for_transcribe("de")
        listener._whisper_backend = "mlx"
        listener._mlx_model_repo = "some/repo"
        fake = MagicMock()
        fake.transcribe.return_value = {"text": "ja"}

        with _patch("jarvis.listening.listener.mlx_whisper", fake, create=True):
            listener._transcribe_security_audio(object())

        assert fake.transcribe.call_args[1]["language"] == "de"

    def test_an_unset_language_still_leaves_detection_on_everywhere(self):
        """The guard must not become "always pin something": an unset
        setting means identification, on every path."""
        listener, model = _listener_for_transcribe("")
        model.transcribe.return_value = (iter([]), MagicMock())

        listener._transcribe_security_audio(object())

        assert model.transcribe.call_args[1]["language"] is None
