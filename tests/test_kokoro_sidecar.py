"""Tests for the Kokoro sidecar's own protocol handling.

jarvis.output.vendor.kokoro_sidecar is the subprocess entry point: the one
place that imports jarvis.output.vendor.kokoro_backtalk (AGPL-3.0) and the
kokoro package. These tests exercise its request/response handling directly,
with jarvis.output.vendor.kokoro_backtalk.stream_kokoro mocked out, so they
never need the real kokoro package installed.
"""

import base64
import io
import json
from unittest.mock import patch

import numpy as np
import pytest


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


class TestHandleSynthesize:
    def test_a_successful_synthesis_emits_chunks_then_end(self):
        from jarvis.output.vendor import kokoro_sidecar

        chunks = [np.array([1, 2, 3], dtype=np.int16), np.array([4, 5], dtype=np.int16)]

        out = io.StringIO()
        with patch("jarvis.output.vendor.kokoro_backtalk.stream_kokoro", return_value=iter(chunks)), \
             patch("sys.stdout", out):
            kokoro_sidecar._handle_synthesize(
                {"id": 7, "text": "hello", "voice": "bm_lewis", "speed": 1.0},
            )

        messages = _lines(out)
        assert [m["type"] for m in messages] == ["chunk", "chunk", "end"]
        assert all(m["id"] == 7 for m in messages)
        decoded = [
            np.frombuffer(base64.b64decode(m["pcm_b64"]), dtype=np.int16)
            for m in messages if m["type"] == "chunk"
        ]
        assert list(decoded[0]) == [1, 2, 3]
        assert list(decoded[1]) == [4, 5]

    def test_a_synthesis_failure_is_reported_as_an_error_message(self):
        from jarvis.output.vendor import kokoro_sidecar

        out = io.StringIO()
        with patch(
            "jarvis.output.vendor.kokoro_backtalk.stream_kokoro",
            side_effect=ImportError("No module named 'kokoro'"),
        ), patch("sys.stdout", out):
            kokoro_sidecar._handle_synthesize(
                {"id": 3, "text": "hello", "voice": "bm_lewis", "speed": 1.0},
            )

        messages = _lines(out)
        assert len(messages) == 1
        assert messages[0]["type"] == "error"
        assert messages[0]["id"] == 3
        assert "kokoro" in messages[0]["message"].lower()

    def test_an_invalid_speed_falls_back_to_normal(self):
        from jarvis.output.vendor import kokoro_sidecar

        captured = {}

        def fake_stream(text, voice, speed):
            captured["speed"] = speed
            return iter([])

        out = io.StringIO()
        with patch("jarvis.output.vendor.kokoro_backtalk.stream_kokoro", side_effect=fake_stream), \
             patch("sys.stdout", out):
            kokoro_sidecar._handle_synthesize(
                {"id": 1, "text": "hi", "voice": "bm_lewis", "speed": "not-a-number"},
            )

        assert captured["speed"] == 1.0


class TestMainLoop:
    def test_a_shutdown_command_ends_the_loop(self):
        from jarvis.output.vendor import kokoro_sidecar

        stdin = io.StringIO(json.dumps({"cmd": "shutdown"}) + "\n")
        out = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", out):
            kokoro_sidecar.main()

        messages = _lines(out)
        assert messages[0]["type"] == "ready"
        # No error should be printed for a clean shutdown command.
        assert not any(m["type"] == "error" for m in messages)

    def test_a_malformed_line_is_reported_and_the_loop_continues(self):
        from jarvis.output.vendor import kokoro_sidecar

        stdin = io.StringIO("not json\n" + json.dumps({"cmd": "shutdown"}) + "\n")
        out = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", out):
            kokoro_sidecar.main()

        messages = _lines(out)
        assert any(m["type"] == "error" and "malformed" in m["message"] for m in messages)

    def test_an_unknown_command_is_reported_and_the_loop_continues(self):
        from jarvis.output.vendor import kokoro_sidecar

        stdin = io.StringIO(
            json.dumps({"cmd": "wave"}) + "\n" + json.dumps({"cmd": "shutdown"}) + "\n",
        )
        out = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", out):
            kokoro_sidecar.main()

        messages = _lines(out)
        assert any(m["type"] == "error" and "unknown command" in m["message"] for m in messages)

    def test_eof_ends_the_loop_without_a_shutdown_command(self):
        from jarvis.output.vendor import kokoro_sidecar

        stdin = io.StringIO("")  # Immediate EOF, as when the parent closes the pipe.
        out = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", out):
            kokoro_sidecar.main()

        messages = _lines(out)
        assert messages == [{"type": "ready"}]
