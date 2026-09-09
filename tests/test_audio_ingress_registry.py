"""
Tests for reaching the running listener's audio ingress from outside it.

The control centre serves the browser microphone and has no reference to
the listener. It reaches the audio path the same way the conversation-mode
switch does: through a registry the listener publishes itself to on start
and clears on shutdown.
"""

import pytest

from jarvis.listening.audio_ingress import (
    audio_ingress_available,
    feed_audio,
    register_audio_sink,
)


class _Sink:
    def __init__(self, accepts: bool = True):
        self.accepts = accepts
        self.received: list[bytes] = []

    def feed_external_audio(self, pcm16: bytes) -> bool:
        self.received.append(pcm16)
        return self.accepts


@pytest.fixture(autouse=True)
def _clear_registry():
    """No test may leak a sink into the next one."""
    register_audio_sink(None)
    yield
    register_audio_sink(None)


class TestAudioIngressRegistry:
    def test_audio_reaches_the_registered_listener(self):
        sink = _Sink()
        register_audio_sink(sink)

        assert feed_audio(b"\x00\x01") is True
        assert sink.received == [b"\x00\x01"]

    def test_no_listener_means_the_frame_is_refused(self):
        """Standalone there is no voice loop, so audio has nowhere to go."""
        assert audio_ingress_available() is False
        assert feed_audio(b"\x00\x01") is False

    def test_availability_follows_registration(self):
        sink = _Sink()
        register_audio_sink(sink)
        assert audio_ingress_available() is True

        register_audio_sink(None)
        assert audio_ingress_available() is False

    def test_a_refusing_listener_is_reported_as_refused(self):
        """A frame dropped for echo or backpressure is not a delivery."""
        register_audio_sink(_Sink(accepts=False))

        assert feed_audio(b"\x00\x01") is False

    def test_a_raising_listener_does_not_reach_the_socket(self):
        """A fault in the audio path must not kill the connection serving it."""
        class _Broken:
            def feed_external_audio(self, pcm16: bytes) -> bool:
                raise RuntimeError("audio path is gone")

        register_audio_sink(_Broken())

        assert feed_audio(b"\x00\x01") is False
