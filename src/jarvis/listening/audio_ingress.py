"""🎙️ The listener's audio ingress, reachable from outside the voice loop.

The microphone can live somewhere other than this process: the control
centre captures it in the browser and posts the frames back. Whoever serves
that capture holds no reference to the listener and must not import it, so
the listener publishes its ingress here on start and clears it on shutdown.

Every function is safe to call when nothing is listening. Standalone there
is no voice loop, and a browser tab left open across a daemon restart will
keep posting for a while; both answer "refused" rather than raising.
"""

from __future__ import annotations

import threading
from typing import Protocol

from ..debug import debug_log


class AudioSink(Protocol):
    def feed_external_audio(self, pcm16: bytes) -> bool: ...


_sink: AudioSink | None = None
_lock = threading.Lock()


def register_audio_sink(sink: AudioSink | None) -> None:
    """Publish the running listener's audio ingress, or clear it on shutdown."""
    global _sink
    with _lock:
        _sink = sink
    debug_log(
        f"audio ingress {'registered' if sink else 'cleared'}",
        "voice",
    )


def audio_ingress_available() -> bool:
    """Whether a listener is currently able to receive posted audio."""
    with _lock:
        return _sink is not None


def feed_audio(pcm16: bytes) -> bool:
    """Hand one frame of 16-bit little-endian mono PCM to the listener.

    Returns whether the frame entered the pipeline. False covers every way
    that can fail to happen: nothing listening, Jarvis is speaking, the
    queue is full, or the audio path raised. The caller is a socket thread
    serving a live capture, so none of those may surface as an exception.
    """
    with _lock:
        sink = _sink
    if sink is None:
        return False
    try:
        return bool(sink.feed_external_audio(pcm16))
    except Exception as exc:
        debug_log(f"audio ingress failed: {type(exc).__name__}", "voice")
        return False
