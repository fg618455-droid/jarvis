"""🎭 Face/visualizer: live state and face gallery config for the vendored UI.

The face itself (``core.js`` and the ``faces/`` gallery under
``jarvis.webui.visualizer.vendor``) is AGPL-3.0-licensed third-party code,
served as static files by the control centre's server. This module is
Jarvis's own glue: the two JSON endpoints the vendored ``core.js`` polls,
answered from Jarvis's own live state rather than the signal files or the
second HTTP server ai-visualizer normally runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, jsonify, Response

from jarvis.config import load_settings
from jarvis.webui.visualizer.state import visualizer_state


bp = Blueprint("visualizer", __name__, url_prefix="/api/visualizer")

VENDOR_DIR = Path(__file__).resolve().parent.parent / "visualizer" / "vendor"
FACES_DIR = VENDOR_DIR / "faces"


def _list_faces() -> list[dict]:
    """The installed faces, discovered the same way ai-visualizer's own
    server.py does: any folder under ``faces/`` carrying an ``index.html``."""
    faces = []
    if not FACES_DIR.is_dir():
        return faces
    for entry in sorted(FACES_DIR.iterdir()):
        if not entry.is_dir() or not (entry / "index.html").exists():
            continue
        meta = {"id": entry.name, "title": entry.name.title(), "tagline": ""}
        try:
            meta.update(json.loads((entry / "face.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        meta["id"] = entry.name
        faces.append(meta)
    return faces


@bp.route("/state")
def state() -> Response:
    """Polled by the face (~8x/sec): the live idle/listening/thinking/speaking
    reading, derived from Jarvis's own runtime phase and TTS playback."""
    return jsonify(visualizer_state())


@bp.route("/config")
def config() -> Response:
    """Display name and the installed face gallery."""
    try:
        name = str(load_settings().wake_word or "jarvis").strip().capitalize()
    except Exception:
        name = "Jarvis"
    return jsonify({
        "name": name,
        "badge": "",
        "face": "board",
        # Jarvis's TTS engines never play a thinking sound of their own, so
        # there is nothing this would ever collide with; the browser's own
        # SND toggle (bottom left of the face) still lets a user turn it off.
        "thinking_sound": True,
        "faces": _list_faces(),
    })
