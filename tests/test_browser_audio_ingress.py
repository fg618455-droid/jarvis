"""
Tests for audio captured outside the process reaching the listening pipeline.

The control centre captures the microphone in the browser and posts the
frames to the daemon. Those frames have to land in the very same queue the
local microphone callback feeds, so that VAD, Whisper, the intent judge and
the reply engine stay unchanged and untested-around.

These tests describe the ingress boundary only: what enters the pipeline and
what is refused. Everything downstream is already covered elsewhere.
"""

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.listening.audio_ingress import audio_ingress_available, register_audio_sink


def _make_listener(tts_speaking: bool = False):
    """Build a VoiceListener with the heavy audio machinery stubbed out."""
    mock_cfg = MagicMock()
    mock_cfg.sample_rate = 16000
    mock_cfg.vad_enabled = False
    mock_cfg.vad_aggressiveness = 2
    mock_cfg.voice_device = None
    mock_cfg.voice_debug = False
    mock_cfg.tune_enabled = False
    mock_cfg.whisper_backend = "faster-whisper"

    mock_tts = MagicMock()
    mock_tts.enabled = True
    mock_tts.is_speaking.return_value = tts_speaking

    with patch("jarvis.listening.listener.webrtcvad", None), \
         patch("jarvis.listening.listener.sd", None), \
         patch("jarvis.listening.listener.create_intent_judge", return_value=None):
        from jarvis.listening.listener import VoiceListener
        listener = VoiceListener(MagicMock(), mock_cfg, mock_tts, MagicMock())

    return listener, mock_tts


def _pcm16(samples):
    """Encode float samples in [-1, 1] as little-endian 16-bit PCM bytes."""
    return np.asarray(samples, dtype="<i2").tobytes()


class TestBrowserAudioIngress:
    """Frames posted by the control centre enter the microphone pipeline."""

    def test_pcm_frame_reaches_the_audio_queue(self):
        """A posted frame arrives as mono float32 the pipeline can consume."""
        listener, _ = _make_listener()

        accepted = listener.feed_external_audio(_pcm16([0, 8192, -8192, 16384]))

        assert accepted is True
        frame = listener._audio_q.get_nowait()
        mono = frame.flatten()
        assert mono.dtype == np.float32
        assert mono.size == 4
        # 8192/32768 == 0.25 — the pipeline measures RMS energy on this scale,
        # so the conversion has to normalise rather than hand over raw counts.
        assert mono[1] == pytest.approx(0.25, abs=1e-4)
        assert mono[2] == pytest.approx(-0.25, abs=1e-4)

    def test_frames_are_dropped_while_jarvis_speaks(self):
        """Echo suppression applies to browser audio exactly as to the mic."""
        listener, _ = _make_listener(tts_speaking=True)

        accepted = listener.feed_external_audio(_pcm16([16384] * 32))

        assert accepted is False
        assert listener._audio_q.empty()

    def test_frames_are_dropped_while_dictating(self):
        """Dictation owns the audio path; browser frames must not race it."""
        listener, _ = _make_listener()
        listener._dictation_active = True

        accepted = listener.feed_external_audio(_pcm16([16384] * 32))

        assert accepted is False
        assert listener._audio_q.empty()

    def test_malformed_payload_is_refused_without_raising(self):
        """A truncated or empty frame is refused, never a crash on the socket."""
        listener, _ = _make_listener()

        assert listener.feed_external_audio(b"") is False
        assert listener.feed_external_audio(b"\x01") is False  # odd byte count
        assert listener._audio_q.empty()

    def test_the_local_microphone_is_not_required(self):
        """No audio device is a downgrade, not the end of the listening loop.

        With the microphone in the browser there may be no local device at
        all, or a blocked one. The loop still has to come up and stay up,
        because the audio it serves arrives over the network.
        """
        listener, _ = _make_listener()
        listener._transcript_buffer = MagicMock()
        listener.state_manager = MagicMock()

        with patch("jarvis.listening.listener.sd", None), \
             patch.object(listener, "_start_llm_warmup", return_value=[]), \
             patch.object(listener, "_load_whisper_model", create=True):
            thread = threading.Thread(target=listener.run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not audio_ingress_available():
                    time.sleep(0.02)
                # The loop is up and reachable even though no device exists.
                assert audio_ingress_available() is True
                assert listener._local_capture is False
                assert thread.is_alive()
            finally:
                listener._should_stop = True
                thread.join(timeout=5.0)
                register_audio_sink(None)

        assert not thread.is_alive()

    def test_a_full_queue_drops_the_frame_instead_of_blocking(self):
        """A slow consumer must not stall the socket thread."""
        listener, _ = _make_listener()
        listener._audio_q = queue.Queue(maxsize=1)
        listener._audio_q.put_nowait(np.zeros(4, dtype=np.float32))

        accepted = listener.feed_external_audio(_pcm16([16384] * 4))

        assert accepted is False
