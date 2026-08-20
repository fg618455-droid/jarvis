"""Mission Control: what a NAS-hosted agent crew has been doing, and a chat.

The crew (Hermes, on Felix' Synology) runs independently of this daemon and
its security gate, on a machine that is not always reachable from here. The
daemon talks to a small NAS-side endpoint rather than opening the crew's own
database directly, so this module is purely a client of that endpoint: a NAS
that is off or unreachable degrades the view to "nothing to show" instead of
a broken page.

The activity feed (``GET /api/crew``) only ever reads. The chat relay
(``POST /api/crew/chat``) forwards one message to one agent and relays the
reply — the NAS-side endpoint proxies it on to the crew's own chat engine,
this module never talks to that engine directly and never persists anything
about the exchange itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import Blueprint, Response, jsonify, request

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.tools.builtin.ask_crew import AGENT_THREADS


bp = Blueprint("crew", __name__, url_prefix="/api")

REQUEST_TIMEOUT_SEC = 3.0
CHAT_TIMEOUT_SEC = 35.0
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
STATUSES = ("success", "failure", "partial")
DAILY_WINDOW_DAYS = 14


def _empty_reply(configured: bool) -> dict[str, Any]:
    return {
        "configured": configured, "reachable": False, "entries": [], "agents": [], "daily": [],
    }


def _tally(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-agent success/failure/partial counts, in the order agents first appear."""
    tallies: dict[str, dict[str, int]] = {}
    for entry in entries:
        name = entry.get("agent_name") or "?"
        counts = tallies.setdefault(name, {status: 0 for status in STATUSES})
        status = entry.get("status")
        if status in STATUSES:
            counts[status] += 1
    return [{"name": name, **counts} for name, counts in tallies.items()]


def _daily_activity(
    entries: list[dict[str, Any]], days: int = DAILY_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Entry counts per calendar day (UTC) over a fixed trailing window, oldest first.

    A fixed window rather than "however many days the entries span" keeps the
    heatmap a stable width regardless of how quiet or busy the crew has been.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        raw = entry.get("created_at")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        day = when.astimezone(timezone.utc).date().isoformat()
        counts[day] = counts.get(day, 0) + 1

    today = datetime.now(timezone.utc).date()
    return [
        {"date": day, "count": counts.get(day, 0)}
        for day in (
            (today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)
        )
    ]


@bp.route("/crew")
def crew() -> Response:
    """Recent crew activity, plus a per-agent tally, or a plain offline state."""
    cfg = load_settings()
    base_url = cfg.crew_api_url
    if not base_url:
        return jsonify(_empty_reply(configured=False))

    try:
        limit = min(MAX_LIMIT, max(1, int(request.args.get("limit", DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    headers = {"X-Crew-Key": cfg.crew_api_key} if cfg.crew_api_key else {}
    try:
        response = requests.get(
            f"{base_url}/agent_logs?limit={limit}",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        entries = response.json().get("entries", [])
    except (requests.exceptions.RequestException, ValueError) as error:
        debug_log(f"the crew endpoint did not answer: {error}", "webui")
        return jsonify(_empty_reply(configured=True))

    return jsonify({
        "configured": True,
        "reachable": True,
        "entries": entries,
        "agents": _tally(entries),
        "daily": _daily_activity(entries),
    })


@bp.route("/crew/chat", methods=["POST"])
def crew_chat() -> Response:
    """Relay one message to one crew agent and return its reply, or say why not."""
    cfg = load_settings()
    base_url = cfg.crew_api_url
    if not base_url:
        return jsonify({"reachable": False, "error": "not configured"})

    body = request.get_json(silent=True) or {}
    agent = str(body.get("agent", "")).strip().lower()
    message = str(body.get("message", "")).strip()

    if agent not in AGENT_THREADS:
        return jsonify({
            "error": f"Unknown crew agent '{agent}'. Choose one of: "
                     f"{', '.join(sorted(AGENT_THREADS))}.",
        }), 400
    if not message:
        return jsonify({"error": "No message given."}), 400

    headers = {"X-Crew-Key": cfg.crew_api_key} if cfg.crew_api_key else {}
    try:
        response = requests.post(
            f"{base_url}/chat",
            headers=headers,
            json={"agent": agent, "message": message},
            timeout=CHAT_TIMEOUT_SEC,
        )
        response.raise_for_status()
        reply = response.json().get("reply", "")
    except (requests.exceptions.RequestException, ValueError) as error:
        debug_log(f"the crew chat endpoint did not answer: {error}", "webui")
        return jsonify({"reachable": False, "error": "The crew channel isn't reachable right now."})

    return jsonify({"reachable": True, "reply": reply})
