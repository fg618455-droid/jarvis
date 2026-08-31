"""Live state, the turn history, and the stream that pushes both."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Iterator

from flask import Blueprint, Response, current_app, jsonify, request

from jarvis.debug import recent_logs
from jarvis.config import load_settings
from jarvis.runtime import (
    get_event_bus,
    get_recorder,
    get_runtime_state,
    read_turn_journal,
)


bp = Blueprint("status", __name__, url_prefix="/api")

# Long enough that an idle connection is not mistaken for a dead one, short
# enough that a closed page is noticed promptly.
KEEP_ALIVE_SECONDS = 15.0


@bp.route("/status")
def status() -> Response:
    """What the assistant is doing, and this session's tallies."""
    return jsonify(_status_snapshot())


def _status_snapshot() -> dict:
    """Return live daemon state, or an honest empty standalone reading."""
    webui = current_app.config["JARVIS_WEBUI"]
    if webui.daemon_attached:
        return {"daemon_running": True, **get_runtime_state().snapshot()}
    return {
        "daemon_running": False,
        "phase": None,
        "phase_since": None,
        "phase_seconds": None,
        "started_at": None,
        "uptime_seconds": None,
        "turns": {"voice": 0, "text": 0, "total": 0},
        "tool_calls": 0,
        "errors": 0,
        "discarded": {},
        "last_error": None,
        "last_error_at": None,
        "last_turn": None,
        "models": {},
        "audio": {},
        "passive": {
            "enabled": False,
            "lines_written": 0,
            "digests_produced": 0,
            "last_line_at": None,
        },
        "conversation": {"active": False},
    }


@bp.route("/logs")
def logs() -> Response:
    """The recent local diagnostic entries, already redacted at capture."""
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    return jsonify({"entries": recent_logs(limit)})


@bp.route("/turns")
def turns() -> Response:
    """The most recent turns, newest last."""
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"turns": _turn_history(limit=max(1, min(limit, 500)))})


@bp.route("/turns/export.csv")
def turns_csv() -> Response:
    """The turn history flattened for a spreadsheet.

    One row per turn with one column per stage, so the shape of the wait can
    be compared across a session without reading JSON.
    """
    history = _turn_history()
    stage_names: list[str] = []
    for turn in history:
        for stage in turn.get("stages", []):
            if stage["name"] not in stage_names:
                stage_names.append(stage["name"])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["turn_id", "started_at", "source", "language", "total_ms", "tools", "error"]
        + [f"{name}_ms" for name in stage_names]
        + ["transcript", "reply"]
    )
    for turn in history:
        durations: dict[str, float] = {}
        for stage in turn.get("stages", []):
            durations[stage["name"]] = durations.get(stage["name"], 0.0) + stage["duration_ms"]
        writer.writerow(
            [
                turn.get("turn_id", ""),
                turn.get("started_at", ""),
                turn.get("source", ""),
                turn.get("language") or "",
                round(turn.get("total_ms") or 0.0, 1),
                " ".join(tool["name"] for tool in turn.get("tools", [])),
                turn.get("error") or "",
            ]
            + [round(durations.get(name, 0.0), 1) for name in stage_names]
            + [turn.get("transcript", ""), turn.get("reply") or ""]
        )

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=jarvis-turns.csv"},
    )


def _turn_history(limit: int | None = None) -> list[dict]:
    """Live daemon turns, or persisted history plus this standalone session."""
    live = get_recorder().history()
    if current_app.config["JARVIS_WEBUI"].daemon_attached:
        return live[-limit:] if limit else live

    cfg = load_settings()
    journal = Path(cfg.db_path).parent / "turns.jsonl"
    combined = read_turn_journal(journal)
    positions = {
        str(turn.get("turn_id")): index
        for index, turn in enumerate(combined)
        if turn.get("turn_id")
    }
    for turn in live:
        turn_id = str(turn.get("turn_id", "") or "")
        if turn_id and turn_id in positions:
            combined[positions[turn_id]] = turn
        else:
            if turn_id:
                positions[turn_id] = len(combined)
            combined.append(turn)
    return combined[-limit:] if limit else combined


@bp.route("/events")
def events() -> Response:
    """Server-sent events: phase changes, stages, finished turns, errors.

    The first message is the current state, so a page that connects mid
    session is correct immediately rather than after the next change.
    """
    initial = _status_snapshot()

    def stream() -> Iterator[str]:
        with get_event_bus().subscribe() as subscription:
            yield _sse("status", initial)
            for event in subscription.listen(timeout=KEEP_ALIVE_SECONDS):
                if event is None:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event["kind"], event["data"])

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Streaming through a proxy that buffers would defeat the point.
            "X-Accel-Buffering": "no",
        },
    )


def _sse(kind: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {kind}\ndata: {payload}\n\n"
