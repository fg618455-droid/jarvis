"""Live state, the turn history, and the stream that pushes both."""

from __future__ import annotations

import csv
import io
import json
from typing import Iterator

from flask import Blueprint, Response, jsonify, request

from jarvis.runtime import get_event_bus, get_recorder, get_runtime_state


bp = Blueprint("status", __name__, url_prefix="/api")

# Long enough that an idle connection is not mistaken for a dead one, short
# enough that a closed page is noticed promptly.
KEEP_ALIVE_SECONDS = 15.0


@bp.route("/status")
def status() -> Response:
    """What the assistant is doing, and this session's tallies."""
    return jsonify(get_runtime_state().snapshot())


@bp.route("/turns")
def turns() -> Response:
    """The most recent turns, newest last."""
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"turns": get_recorder().history(limit=max(1, min(limit, 500)))})


@bp.route("/turns/export.csv")
def turns_csv() -> Response:
    """The turn history flattened for a spreadsheet.

    One row per turn with one column per stage, so the shape of the wait can
    be compared across a session without reading JSON.
    """
    history = get_recorder().history()
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


@bp.route("/events")
def events() -> Response:
    """Server-sent events: phase changes, stages, finished turns, errors.

    The first message is the current state, so a page that connects mid
    session is correct immediately rather than after the next change.
    """
    def stream() -> Iterator[str]:
        with get_event_bus().subscribe() as subscription:
            yield _sse("status", get_runtime_state().snapshot())
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
