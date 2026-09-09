"""
End-to-end test of the microphone socket against a real server.

The origin policy is unit-tested next door. This one answers the question
those tests cannot: does a browser-shaped WebSocket actually reach the
listener's audio queue through werkzeug, the request guards and the
blueprint, and does a refused origin really fail to.
"""

import socket
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")

from jarvis.listening.audio_ingress import register_audio_sink
from jarvis.webui.server import WebUIConfig, WebUIServer


class _RecordingSink:
    def __init__(self):
        self.frames: list[bytes] = []

    def feed_external_audio(self, pcm16: bytes) -> bool:
        self.frames.append(pcm16)
        return True


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    port = _free_port()
    cfg = WebUIConfig(host="127.0.0.1", port=port, token="")
    srv = WebUIServer(cfg)
    srv.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.02)
    yield port
    srv.stop()


@pytest.fixture
def sink():
    recorder = _RecordingSink()
    register_audio_sink(recorder)
    yield recorder
    register_audio_sink(None)


def test_captured_audio_reaches_the_listener(server, sink):
    """The whole path: browser frame in, listener queue out."""
    ws = simple_websocket.Client(
        f"ws://127.0.0.1:{server}/api/voice/stream",
        headers={"Origin": f"http://127.0.0.1:{server}"},
    )
    try:
        ws.send(b"\x00\x01\x02\x03")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sink.frames:
            time.sleep(0.02)
    finally:
        ws.close()

    assert sink.frames == [b"\x00\x01\x02\x03"]


def test_a_hostile_origin_never_opens_the_socket(server, sink):
    """The attack the origin check exists for, exercised for real."""
    with pytest.raises(simple_websocket.ConnectionError) as refused:
        simple_websocket.Client(
            f"ws://127.0.0.1:{server}/api/voice/stream",
            headers={"Origin": "https://evil.example"},
        )

    # Refused by the origin check, not by some unrelated failure that would
    # let this test keep passing after the check was removed.
    assert "403" in str(refused.value)
    assert sink.frames == []


def test_oversized_frames_are_dropped(server, sink):
    """A frame far larger than any capture chunk never reaches the pipeline."""
    from jarvis.webui.api.voice import MAX_FRAME_BYTES

    ws = simple_websocket.Client(
        f"ws://127.0.0.1:{server}/api/voice/stream",
        headers={"Origin": f"http://127.0.0.1:{server}"},
    )
    try:
        ws.send(b"\x00" * (MAX_FRAME_BYTES + 2))
        ws.send(b"\x10\x20")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sink.frames:
            time.sleep(0.02)
    finally:
        ws.close()

    # Only the small frame survived; the oversized one was refused silently.
    assert sink.frames == [b"\x10\x20"]
