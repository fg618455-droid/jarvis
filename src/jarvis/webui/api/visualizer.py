"""🎭 Face: the live state the deck's face is drawn from.

One endpoint, answered from Jarvis's own runtime phase and TTS playback. The
face itself is drawn by the control centre in `static/js/face.js`; this is
only the reading it draws.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, Response

from jarvis.webui.visualizer.state import visualizer_state


bp = Blueprint("visualizer", __name__, url_prefix="/api/visualizer")


@bp.route("/state")
def state() -> Response:
    """Polled by the face (~8x/sec): the live idle/listening/thinking/speaking
    reading, derived from Jarvis's own runtime phase and TTS playback."""
    return jsonify(visualizer_state())
