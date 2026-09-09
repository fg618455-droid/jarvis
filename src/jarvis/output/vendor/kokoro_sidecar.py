"""Kokoro TTS sidecar entry point.

Runs as its own subprocess (``python -m jarvis.output.vendor.kokoro_sidecar``),
launched lazily by ``jarvis.output.kokoro_sidecar_client.KokoroSidecarClient``
only when ``tts_engine`` is ``"kokoro"`` and speech is first actually
requested. This is the process boundary the AGPL-3.0-licensed
``jarvis.output.vendor.kokoro_backtalk`` module and the ``kokoro`` package
live behind: nothing in the main daemon process imports either.

This file is original Jarvis code (the sidecar protocol loop), not vendored
from backtalk; it lives beside ``kokoro_backtalk.py`` because its only job is
to run that module in isolation.

Protocol: newline-delimited JSON on stdin/stdout, one JSON object per line.

Request (stdin), one per line:
    {"cmd": "synthesize", "id": <int>, "text": <str>, "voice": <str>, "speed": <float>}
    {"cmd": "shutdown"}

Response (stdout):
    {"type": "ready"}                                   — printed once at startup
    {"type": "chunk", "id": <int>, "pcm_b64": <str>}     — one per Kokoro-yielded chunk
    {"type": "end", "id": <int>}                         — synthesis for this id finished
    {"type": "error", "id": <int|null>, "message": <str>} — this request failed

A synthesis failure (missing ``kokoro`` package, a model download failure, a
malformed request) never exits the process: it is reported as an ``error``
message and the sidecar keeps reading requests. The sidecar only exits on a
``shutdown`` command or when stdin reaches EOF (the parent process closed the
pipe, e.g. because it exited without an explicit shutdown).
"""

from __future__ import annotations

import base64
import json
import sys


def _emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _handle_synthesize(request: dict) -> None:
    request_id = request.get("id")
    text = str(request.get("text") or "")
    voice = str(request.get("voice") or "bm_lewis")
    try:
        speed = float(request.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0

    try:
        from .kokoro_backtalk import stream_kokoro

        for chunk in stream_kokoro(text, voice, speed):
            _emit({
                "type": "chunk",
                "id": request_id,
                "pcm_b64": base64.b64encode(chunk.tobytes()).decode("ascii"),
            })
        _emit({"type": "end", "id": request_id})
    except Exception as exc:  # kokoro not installed, model download failed, synth error
        _emit({
            "type": "error",
            "id": request_id,
            "message": f"{type(exc).__name__}: {exc}",
        })


def main() -> None:
    _emit({"type": "ready"})
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            _emit({"type": "error", "id": None, "message": f"malformed request: {exc}"})
            continue

        cmd = request.get("cmd")
        if cmd == "shutdown":
            break
        elif cmd == "synthesize":
            _handle_synthesize(request)
        else:
            _emit({
                "type": "error",
                "id": request.get("id"),
                "message": f"unknown command: {cmd!r}",
            })


if __name__ == "__main__":
    main()
