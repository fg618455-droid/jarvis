"""Local subprocess boundary for the Kokoro TTS engine.

``jarvis.output.vendor.kokoro_backtalk`` (AGPL-3.0, vendored from backtalk)
and its ``kokoro`` package dependency are never imported here, or anywhere
else in the main daemon process. :class:`KokoroSidecarClient` launches
``jarvis.output.vendor.kokoro_sidecar`` as a subprocess, lazily on first
actual use, and talks to it over its stdin/stdout pipes with one
newline-delimited JSON message per line. This keeps the AGPL code and its
PyTorch dependency in their own process: the same separable,
process-boundary shape the Face/visualizer view already has over the
network (see ``THIRD_PARTY_NOTICES.md``).
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
from typing import Iterator, Optional

import numpy as np

from ..debug import debug_log

# Kokoro's fixed output sample rate. A constant here rather than an import
# from the vendor module, so nothing on the client side needs it installed.
KOKORO_RATE = 24000

_SIDECAR_MODULE = "jarvis.output.vendor.kokoro_sidecar"


class KokoroSidecarError(Exception):
    """The sidecar could not be reached, crashed, or reported a failure."""


class KokoroSidecarClient:
    """Launches and talks to the Kokoro sidecar subprocess.

    One utterance is synthesised at a time, the same shape
    :class:`jarvis.output.tts.KokoroTTS`'s own worker thread already drives,
    so this holds no request queue of its own: :meth:`synthesize` blocks the
    calling thread until the sidecar answers, and is only ever called from
    that one worker thread.
    """

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _ensure_started(self) -> None:
        if self.is_running:
            return
        with self._lock:
            if self.is_running:
                return
            debug_log("launching Kokoro TTS sidecar", "tts")
            process = subprocess.Popen(
                [sys.executable, "-m", _SIDECAR_MODULE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            ready_line = process.stdout.readline() if process.stdout else ""
            if not ready_line:
                stderr = process.stderr.read().strip() if process.stderr else ""
                debug_log(f"Kokoro TTS sidecar failed to start: {stderr}", "tts")
                raise KokoroSidecarError(
                    f"Kokoro sidecar exited before starting: {stderr}"
                )
            try:
                ready = json.loads(ready_line)
            except ValueError as exc:
                raise KokoroSidecarError(
                    f"Kokoro sidecar sent an unreadable ready message: {exc}"
                ) from exc
            if ready.get("type") != "ready":
                raise KokoroSidecarError(
                    f"Kokoro sidecar did not report ready: {ready_line.strip()}"
                )
            self._process = process
            debug_log("Kokoro TTS sidecar ready", "tts")

    def synthesize(self, text: str, voice: str, speed: float) -> Iterator["np.ndarray"]:
        """One utterance -> int16 PCM chunks at 24kHz, from the sidecar.

        Chunks are yielded as the sidecar sends them, one Kokoro-yielded
        chunk at a time, so playback can start on the first chunk instead of
        waiting for the whole utterance to cross the process boundary.

        Raises :class:`KokoroSidecarError` on a crash, a missing/broken
        ``kokoro`` install, or any other synthesis failure the sidecar
        reports. The process is dropped on failure so the next call
        relaunches a fresh one rather than reusing a wedged pipe.
        """
        self._ensure_started()
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise KokoroSidecarError("Kokoro sidecar is not running")

        with self._lock:
            self._next_id += 1
            request_id = self._next_id

        request = {
            "cmd": "synthesize",
            "id": request_id,
            "text": text,
            "voice": voice,
            "speed": speed,
        }
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._drop()
            debug_log(f"Kokoro TTS sidecar pipe closed on write: {exc}", "tts")
            raise KokoroSidecarError(f"Kokoro sidecar pipe closed: {exc}") from exc

        while True:
            line = process.stdout.readline()
            if not line:
                stderr = process.stderr.read().strip() if process.stderr else ""
                self._drop()
                debug_log(f"Kokoro TTS sidecar exited mid-synthesis: {stderr}", "tts")
                raise KokoroSidecarError(
                    f"Kokoro sidecar exited mid-synthesis: {stderr}"
                )
            try:
                message = json.loads(line)
            except ValueError as exc:
                raise KokoroSidecarError(
                    f"Kokoro sidecar sent an unreadable message: {exc}"
                ) from exc

            if message.get("id") not in (None, request_id):
                # A stray reply for an earlier request (should not happen,
                # since requests are strictly serial); ignore and keep
                # reading for this one.
                continue

            kind = message.get("type")
            if kind == "chunk":
                pcm_bytes = base64.b64decode(message["pcm_b64"])
                yield np.frombuffer(pcm_bytes, dtype=np.int16)
            elif kind == "end":
                return
            elif kind == "error":
                debug_log(f"Kokoro TTS sidecar reported an error: {message.get('message')}", "tts")
                raise KokoroSidecarError(
                    message.get("message") or "Kokoro sidecar reported an error"
                )
            # Any other message type is ignored rather than treated as fatal.

    def _drop(self) -> None:
        if self._process is not None:
            try:
                self._process.kill()
            except Exception:
                pass
        self._process = None

    def stop(self) -> None:
        """Ask the sidecar to exit, then make sure it actually does."""
        process = self._process
        if process is None:
            return
        debug_log("stopping Kokoro TTS sidecar", "tts")
        try:
            if process.stdin is not None:
                process.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
        self._process = None
