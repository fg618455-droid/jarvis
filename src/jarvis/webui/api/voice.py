"""🎙️ The microphone socket: speech captured in the browser, streamed here.

The page records the microphone and sends 16-bit mono PCM frames over one
long-lived connection. They go straight into the listener's audio queue, so
VAD, transcription, the intent judge and the reply engine see them exactly
as they see the local microphone.

Why this endpoint guards itself: a WebSocket upgrade is a GET, so the
write-header check does not apply to it, and WebSockets are not subject to
CORS at all. Any page in any tab can open one against a loopback port. This
socket speaks into an assistant that runs tools, so it checks the Origin
itself instead of inheriting the ordinary request guards.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from jarvis.debug import debug_log
from jarvis.listening.audio_ingress import audio_ingress_available, feed_audio


bp = Blueprint("voice", __name__, url_prefix="/api")

# One frame is 16-bit mono at the listener's sample rate. A second of audio
# at 16 kHz is 32 kB, so anything approaching this is not a capture frame.
MAX_FRAME_BYTES = 64 * 1024


def origin_allowed(origin: Optional[str], bind_host: str, port: int) -> bool:
    """Whether a browser at ``origin`` may open the microphone socket.

    Absent means a non-browser client: allowed, because nothing can trick a
    script or a CLI into sending someone else's Origin. Present means a page,
    and it has to be this control centre, on this host, on this port.

    ``null`` is what a sandboxed frame sends. It names no site that could be
    checked, so it is refused rather than read as "no browser here".
    """
    if origin is None or origin == "":
        return True
    if origin == "null":
        return False

    try:
        parts = urlsplit(origin)
        # A netloc like "127.0.0.1:5055.evil.example" parses, but reading the
        # port raises. Anything we cannot read exactly is not this origin.
        origin_port = parts.port
        host = parts.hostname
    except ValueError:
        return False

    if parts.scheme not in ("http", "https") or not host:
        return False
    if origin_port != port:
        return False

    # Deferred: server.py imports crew_stream -> api.crew, and importing
    # this package's __init__ eagerly imports every submodule including
    # this one, so a module-level import here would run before server.py
    # finishes defining the name.
    from ..server import is_loopback_host

    if is_loopback_host(bind_host):
        return is_loopback_host(host)
    return host == bind_host


def _cfg():
    """The control centre's own config, as installed by create_app."""
    return current_app.config["JARVIS_WEBUI"]


@bp.route("/voice/status")
def voice_status():
    """Whether speaking through the browser can work right now."""
    return jsonify({
        "ingress": audio_ingress_available(),
        "sample_rate": 16000,
        "frame_bytes_max": MAX_FRAME_BYTES,
    })


@bp.route("/voice/stream", websocket=True)
def voice_stream():
    """Carry captured microphone frames into the listening pipeline."""
    import simple_websocket

    cfg = _cfg()
    origin = request.headers.get("Origin")
    if not origin_allowed(origin, cfg.host, cfg.port):
        debug_log(f"voice socket refused an origin: {origin!r}", "webui")
        return jsonify(error="origin not allowed"), 403

    ws = simple_websocket.Server(request.environ)
    debug_log("voice socket opened", "voice")
    frames = 0
    accepted = 0
    try:
        while True:
            message = ws.receive()
            if message is None:
                break
            if isinstance(message, str):
                # The only text the page sends is a keep-alive ping. Control
                # for the turn itself lives in the ordinary JSON endpoints.
                continue
            if len(message) > MAX_FRAME_BYTES:
                debug_log(f"voice socket dropped an oversized frame: {len(message)} bytes", "voice")
                continue
            frames += 1
            if feed_audio(message):
                accepted += 1
    except simple_websocket.ConnectionClosed:
        pass
    finally:
        debug_log(f"voice socket closed after {frames} frames, {accepted} accepted", "voice")
        try:
            ws.close()
        except Exception:
            pass
    return ""
