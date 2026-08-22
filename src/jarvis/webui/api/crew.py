"""Mission Control: what a NAS-hosted agent crew has been doing.

The crew (Hermes, on Felix' Synology) runs independently of this daemon and
its security gate, on a machine that is not always reachable from here. The
daemon reads a small read-only endpoint the NAS exposes rather than opening
the crew's own database directly, so this module is purely a client: it
never writes anything back, and a NAS that is off or unreachable degrades
the view to "nothing to show" instead of a broken page.

The reading is shaped here rather than in the browser, because the crew
poller publishes the same shape over the event bus and both must agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import Blueprint, Response, jsonify, request

from jarvis.config import Settings, load_settings
from jarvis.debug import debug_log


bp = Blueprint("crew", __name__, url_prefix="/api")

REQUEST_TIMEOUT_SEC = 3.0
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
STATUSES = ("success", "failure", "partial")
DAILY_WINDOW_DAYS = 14


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _empty_reply(configured: bool) -> dict[str, Any]:
    return {
        "configured": configured, "reachable": False, "checked_at": _now(),
        "entries": [], "agents": [], "daily": [],
    }


def _window(days: int = DAILY_WINDOW_DAYS) -> list[str]:
    """The calendar days the reading covers, oldest first.

    A fixed window rather than "however many days the entries span" keeps
    the activity ribbon a stable width regardless of how quiet or busy the
    crew has been.
    """
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _day_of(entry: dict[str, Any]) -> str | None:
    """The UTC calendar day an entry was logged on, or None if it has no date."""
    raw = entry.get("created_at")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).date().isoformat()


def _daily_activity(entries: list[dict[str, Any]], days: list[str]) -> list[dict[str, Any]]:
    """Entry counts per calendar day over the shared window, zero-filled.

    Split by outcome as well as totalled, because a busy day and a day of
    failures are not the same reading and a single bar cannot say which one
    happened.
    """
    tally = {day: {"count": 0, **{status: 0 for status in STATUSES}} for day in days}
    for entry in entries:
        day = _day_of(entry)
        if day not in tally:
            continue
        tally[day]["count"] += 1
        status = entry.get("status")
        if status in STATUSES:
            tally[day][status] += 1
    return [{"date": day, **tally[day]} for day in days]


def _roster(entries: list[dict[str, Any]], configured: list[str]) -> list[str]:
    """Who to report on: the configured crew, plus anyone else who logged work.

    The log only names agents that have done something, so a roster is the
    only way an idle agent reads as quiet rather than as nonexistent. An
    agent that logs work without being listed is appended rather than
    dropped, because hiding real activity is the worse failure.
    """
    names = list(configured)
    known = set(names)
    for entry in entries:
        name = entry.get("agent_name") or "?"
        if name not in known:
            known.add(name)
            names.append(name)
    return names


def _agents(
    entries: list[dict[str, Any]], configured: list[str], days: list[str],
) -> list[dict[str, Any]]:
    """Per-agent tallies, last outcome, and daily counts over the shared window.

    ``daily`` is a bare list of counts positioned against the same days as
    the reply's own ``daily``, so an agent's ribbon lines up with the crew's
    without repeating fourteen dates seven times over.
    """
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_agent.setdefault(entry.get("agent_name") or "?", []).append(entry)

    agents = []
    for name in _roster(entries, configured):
        logged = by_agent.get(name, [])
        counts = {status: 0 for status in STATUSES}
        per_day = {day: 0 for day in days}
        for entry in logged:
            status = entry.get("status")
            if status in STATUSES:
                counts[status] += 1
            day = _day_of(entry)
            if day in per_day:
                per_day[day] += 1

        # The endpoint returns newest first, so the first entry an agent has
        # is its most recent one.
        latest = logged[0] if logged else None
        agents.append({
            "name": name,
            **counts,
            "total": len(logged),
            "last_at": (latest or {}).get("created_at"),
            "last_status": (latest or {}).get("status"),
            "daily": [per_day[day] for day in days],
        })
    return agents


def crew_snapshot(cfg: Settings, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """One reading of the crew's activity, or a plain offline state.

    A connection failure, a timeout, and a reply that does not parse are all
    the same answer: the NAS is not reachable. Nothing here invents a
    reading it does not have.
    """
    if not cfg.crew_api_url:
        return _empty_reply(configured=False)

    headers = {"X-Crew-Key": cfg.crew_api_key} if cfg.crew_api_key else {}
    try:
        response = requests.get(
            f"{cfg.crew_api_url}/agent_logs?limit={limit}",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        entries = response.json().get("entries", [])
    except (requests.exceptions.RequestException, ValueError) as error:
        debug_log(f"the crew endpoint did not answer: {error}", "webui")
        return _empty_reply(configured=True)

    days = _window()
    return {
        "configured": True,
        "reachable": True,
        "checked_at": _now(),
        "entries": entries,
        "agents": _agents(entries, cfg.crew_agents, days),
        "daily": _daily_activity(entries, days),
    }


@bp.route("/crew")
def crew() -> Response:
    """The current reading, taken on demand for a page that has just opened."""
    try:
        limit = min(MAX_LIMIT, max(1, int(request.args.get("limit", DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    return jsonify(crew_snapshot(load_settings(), limit))
