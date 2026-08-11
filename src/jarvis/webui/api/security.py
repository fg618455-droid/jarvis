"""The confirmation policy, what is waiting on it, and what it decided."""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from jarvis.config import load_settings
from jarvis.security.gate import VALID_LEVELS, SecurityGate
from jarvis.security.web_confirm import get_web_confirmations


bp = Blueprint("security", __name__, url_prefix="/api/security")


@bp.route("")
def overview() -> Response:
    """The level in force, which channels can answer, and the log."""
    cfg = load_settings()
    gate = SecurityGate.get_instance()
    confirmations = get_web_confirmations()

    channels = []
    for name in cfg.security_confirm_channels:
        channel = gate._channels.get(name) if gate is not None else None
        available = None
        if channel is not None:
            try:
                available = bool(channel.is_available)
            except Exception:
                available = False
        channels.append({"name": name, "available": available})

    return jsonify({
        "level": cfg.security_level,
        "levels": sorted(VALID_LEVELS),
        "timeout_seconds": cfg.security_confirmation_timeout_sec,
        "channels": channels,
        "pending": confirmations.pending(),
        "decisions": confirmations.decisions(),
    })


@bp.route("/pending")
def pending() -> Response:
    """Requests waiting for a decision right now."""
    return jsonify({"pending": get_web_confirmations().pending()})


@bp.route("/decide", methods=["POST"])
def decide() -> Response:
    """Approve or refuse one waiting request."""
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get("request_id") or "").strip()
    approved = bool(payload.get("approved", False))

    if not request_id:
        return jsonify(error="request_id is required"), 400
    if not get_web_confirmations().decide(request_id, approved):
        # Already answered, or it timed out while the page was thinking.
        return jsonify(error="no such pending request"), 404
    return jsonify({"request_id": request_id, "approved": approved})
