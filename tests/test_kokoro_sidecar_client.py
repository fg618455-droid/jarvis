"""Tests for the main-process side of the Kokoro sidecar boundary.

KokoroSidecarClient never imports jarvis.output.vendor.kokoro_backtalk or the
kokoro package: it only launches jarvis.output.vendor.kokoro_sidecar as a
subprocess and talks to it over stdin/stdout. subprocess.Popen is mocked
throughout, mirroring how tests/tools/builtin/test_system_manager.py asserts
the exact subprocess call rather than only "it worked".
"""

import base64
import json
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _fake_process(stdout_lines, stderr_text: str = ""):
    """A MagicMock standing in for subprocess.Popen's return value.

    ``stdout_lines`` is consumed one readline() call at a time; the last
    "" simulates EOF once every real line has been read.
    """
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdout = MagicMock()
    process.stdout.readline.side_effect = list(stdout_lines) + [""]
    process.stderr = MagicMock()
    process.stderr.read.return_value = stderr_text
    process.poll.return_value = None
    return process


def _line(payload: dict) -> str:
    return json.dumps(payload) + "\n"


class TestLazyLaunch:
    def test_the_subprocess_is_not_spawned_on_construction(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen") as popen:
            KokoroSidecarClient()

            popen.assert_not_called()

    def test_the_first_synthesize_call_launches_the_sidecar_module(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        process = _fake_process([_line({"type": "ready"}), _line({"type": "end", "id": 1})])
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process) as popen:
            client = KokoroSidecarClient()
            list(client.synthesize("hello", "bm_lewis", 1.0))

        popen.assert_called_once_with(
            [sys.executable, "-m", "jarvis.output.vendor.kokoro_sidecar"],
            stdin=-1, stdout=-1, stderr=-1, text=True, bufsize=1,
        )

    def test_a_second_call_reuses_the_running_process(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        process = _fake_process([
            _line({"type": "ready"}),
            _line({"type": "end", "id": 1}),
            _line({"type": "end", "id": 2}),
        ])
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process) as popen:
            client = KokoroSidecarClient()
            list(client.synthesize("hello", "bm_lewis", 1.0))
            list(client.synthesize("world", "bm_lewis", 1.0))

        popen.assert_called_once()


class TestSynthesize:
    def test_chunks_are_decoded_and_yielded_in_order(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        chunk_a = base64.b64encode(np.array([1, 2, 3], dtype=np.int16).tobytes()).decode("ascii")
        chunk_b = base64.b64encode(np.array([4, 5], dtype=np.int16).tobytes()).decode("ascii")
        process = _fake_process([
            _line({"type": "ready"}),
            _line({"type": "chunk", "id": 1, "pcm_b64": chunk_a}),
            _line({"type": "chunk", "id": 1, "pcm_b64": chunk_b}),
            _line({"type": "end", "id": 1}),
        ])
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process):
            client = KokoroSidecarClient()
            chunks = list(client.synthesize("hello", "bm_lewis", 1.0))

        assert [list(c) for c in chunks] == [[1, 2, 3], [4, 5]]

    def test_the_request_carries_the_text_voice_and_speed(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        process = _fake_process([_line({"type": "ready"}), _line({"type": "end", "id": 1})])
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process):
            client = KokoroSidecarClient()
            list(client.synthesize("good evening", "jf_alpha", 1.2))

        sent = json.loads(process.stdin.write.call_args_list[0].args[0])
        assert sent["cmd"] == "synthesize"
        assert sent["text"] == "good evening"
        assert sent["voice"] == "jf_alpha"
        assert sent["speed"] == 1.2
        process.stdin.flush.assert_called()


class TestFailureHandling:
    def test_an_error_message_raises_kokoro_sidecar_error(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient, KokoroSidecarError

        process = _fake_process([
            _line({"type": "ready"}),
            _line({"type": "error", "id": 1, "message": "ImportError: No module named 'kokoro'"}),
        ])
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process):
            client = KokoroSidecarClient()
            with pytest.raises(KokoroSidecarError, match="kokoro"):
                list(client.synthesize("hello", "bm_lewis", 1.0))

    def test_a_process_that_never_becomes_ready_raises(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient, KokoroSidecarError

        process = _fake_process([], stderr_text="Traceback: boom")
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process):
            client = KokoroSidecarClient()
            with pytest.raises(KokoroSidecarError):
                list(client.synthesize("hello", "bm_lewis", 1.0))

    def test_a_process_that_dies_mid_synthesis_raises_and_drops_the_process(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient, KokoroSidecarError

        process = _fake_process([_line({"type": "ready"})], stderr_text="segfault")
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process):
            client = KokoroSidecarClient()
            with pytest.raises(KokoroSidecarError):
                list(client.synthesize("hello", "bm_lewis", 1.0))

            assert not client.is_running

    def test_a_dead_process_is_relaunched_on_the_next_call(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient, KokoroSidecarError

        dead_process = _fake_process([_line({"type": "ready"})], stderr_text="crash")
        healthy_process = _fake_process([_line({"type": "ready"}), _line({"type": "end", "id": 2})])

        with patch(
            "jarvis.output.kokoro_sidecar_client.subprocess.Popen",
            side_effect=[dead_process, healthy_process],
        ) as popen:
            client = KokoroSidecarClient()
            with pytest.raises(KokoroSidecarError):
                list(client.synthesize("hello", "bm_lewis", 1.0))

            list(client.synthesize("hello again", "bm_lewis", 1.0))

        assert popen.call_count == 2


class TestStop:
    def test_stop_without_ever_starting_is_a_no_op(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        client = KokoroSidecarClient()
        client.stop()  # must not raise

    def test_stop_sends_a_shutdown_command_and_waits(self):
        from jarvis.output.kokoro_sidecar_client import KokoroSidecarClient

        process = _fake_process([_line({"type": "ready"}), _line({"type": "end", "id": 1})])
        with patch("jarvis.output.kokoro_sidecar_client.subprocess.Popen", return_value=process):
            client = KokoroSidecarClient()
            list(client.synthesize("hello", "bm_lewis", 1.0))
            client.stop()

        sent_lines = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        assert sent_lines[-1] == {"cmd": "shutdown"}
        process.wait.assert_called_once()
        assert not client.is_running
