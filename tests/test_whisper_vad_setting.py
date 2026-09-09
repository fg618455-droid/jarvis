"""Behaviour tests for the `whisper_vad` setting reaching transcription.

Whisper invents a stock phrase out of room noise: measured on this codebase's
own warmup noise it produces "Untertitelung des ZDF, 2020" with a
`no_speech_prob` of 0.000, which no downstream filter can tell from speech.
Whisper's own VAD filter discards that audio before the decoder ever sees it.

The setting is offered to users and defaults on, so the transcription calls
have to read it rather than pin the filter off.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest


def _listener_with(whisper_vad):
    import numpy as np
    import time

    model = MagicMock()
    segment = MagicMock()
    segment.text = "guten morgen"
    info = MagicMock()
    info.language = "de"
    info.language_probability = 1.0
    model.transcribe.return_value = (iter([segment]), info)

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
                cfg.whisper_language = ""
                cfg.whisper_vad = whisper_vad

                listener = VoiceListener(MagicMock(), cfg, MagicMock(), MagicMock())
                listener.model = model
                listener._whisper_backend = "faster-whisper"
                listener._whisper_device = "cuda"
                listener._samplerate = 16000

                listener._utterance_frames = [np.zeros(16000, dtype=np.float32)]
                listener.echo_detector._utterance_start_time = time.time() - 1.0
                listener.is_speech_active = True

                return listener, model


class TestListenerHonoursTheSetting:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_setting_reaches_whisper(self, enabled):
        listener, model = _listener_with(enabled)

        listener._finalize_utterance()

        assert model.transcribe.call_args[1]["vad_filter"] is enabled


class TestDictationHonoursTheSetting:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_setting_reaches_whisper(self, enabled):
        from jarvis.dictation.dictation_engine import DictationEngine

        cfg = MagicMock()
        cfg.whisper_language = ""
        cfg.whisper_vad = enabled
        engine = DictationEngine(
            whisper_model_ref=lambda: None,
            whisper_backend_ref=lambda: "faster-whisper",
            mlx_repo_ref=lambda: None,
            transcribe_lock=threading.Lock(),
            cfg=cfg,
        )
        model = MagicMock()
        model.transcribe.return_value = (iter([]), MagicMock())

        engine._transcribe_faster_whisper(model, object())

        assert model.transcribe.call_args[1]["vad_filter"] is enabled
