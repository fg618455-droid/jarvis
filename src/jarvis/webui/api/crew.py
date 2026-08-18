"""Mission Control: what a NAS-hosted agent crew has been doing.

The crew (Hermes, on Felix' Synology) runs independently of this daemon and
its security gate, on a machine that is not always reachable from here. The
daemon reads a small read-only endpoint the NAS exposes rather than opening
the crew's own database directly, so this module is purely a client: it
never writes anything back, and a NAS that is off or unreachable degrades
the view to "nothing to show" instead of a broken page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import Blueprint, Response, jsonify, request

from jarvis.config import load_settings
from jarvis.debug import debug_log


bp = Blueprint("crew", __name__, url_prefix="/api")

REQUEST_TIMEOUT_SEC = 3.0
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
