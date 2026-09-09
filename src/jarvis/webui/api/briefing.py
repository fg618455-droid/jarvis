"""📋 The briefing: what is on today, on a screen rather than in the air.

The assistant already has a morning briefing. Once per local day, at a
configured time, it reads the School branch and speaks the result. That is
the right shape for something that should find you without being asked, and
the wrong shape for something you want to check: speech has happened or it
has not, it cannot be re-read, and before the trigger time it does not exist.

This is the same question put to the same source. It shares the branch reader
and the generator with ``jarvis.memory.morning_briefing`` deliberately: two
briefings phrased by two prompts would eventually disagree about the same
day, and the one you could not re-read would be the one you half remembered.

What differs is only what a screen can do that a speaker cannot.

| Reading | Cost | When |
|---|---|---|
| The items | A bounded graph read, no model | Every request |
| The prose | One CHAT-tier call | Only when asked, then cached for the local day |

The split is the whole design. A widget on the deck repaints every ten
seconds; a briefing that generated prose on every repaint would run a
CHAT-tier model six times a minute for a card three lines tall. The items are
extracted deterministically and cost nothing, so they are always current. The
prose is asked for.

Nothing here invents a school. An empty branch reads as empty and is never
sent to a model; a generation that fails says so.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.memory.db import Database
from jarvis.memory.morning_briefing import generate_morning_briefing
from jarvis.memory.school_context import read_school_branch, school_local_now


bp = Blueprint("briefing", __name__, url_prefix="/api/briefing")

# Where the day's generated prose is kept. The spoken briefing's own gate
# lives beside it under `morning_briefing.`; these are deliberately separate
# keys, because reading a briefing on screen must never persuade the spoken
# one that it has already delivered today.
_SUMMARY_KEY = "briefing.web_summary"
_SUMMARY_DATE_KEY = "briefing.web_summary_date"

_SPOKEN_DELIVERED_KEY = "morning_briefing.last_delivered_local_date"

# One item is a line in a widget and a row in a panel, not an essay.
_NOTE_MAX_CHARS = 240


def _config():
    try:
        return load_settings()
    except Exception:
        return None


def _db(cfg) -> Database | None:
    try:
        return Database(cfg.db_path)
    except Exception as exc:
        debug_log(f"briefing could not open memory: {type(exc).__name__}", "webui")
        return None


def _items(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """The School branch as lines, without a model in the way.

    A node is worth showing when it has a name to show it under. Its
    description and its stored facts are the same kind of thing to a reader,
    so they are joined rather than presented as two fields the reader has to
    reconcile.
    """
    items = []
    for node in snapshot.get("nodes") or []:
        title = str(node.get("name") or "").strip()
        if not title:
            continue
        note = " · ".join(
            part for part in (
                str(node.get("description") or "").strip(),
                str(node.get("data") or "").strip(),
            ) if part
        )
        items.append({"title": title, "note": note[:_NOTE_MAX_CHARS]})
    return items


def _spoken(cfg, db: Database | None) -> dict[str, Any]:
    """What the once-a-day spoken briefing is set to do, and when it last did."""
    delivered = None
    if db is not None:
        try:
            delivered = db.get_app_state(_SPOKEN_DELIVERED_KEY)
        except Exception:
            delivered = None
    return {
        "enabled": bool(getattr(cfg, "morning_briefing_enabled", False)),
        "time": str(getattr(cfg, "morning_briefing_time", "07:00") or "07:00"),
        "last_delivered": delivered,
    }


def _today(cfg) -> str:
    try:
        return school_local_now(cfg).date().isoformat()
    except Exception:
        from datetime import date

        return date.today().isoformat()


def _cached_summary(db: Database | None, today: str) -> str:
    """Today's prose, and only today's.

    A summary written yesterday describes yesterday. It is not shown and not
    deleted: the next refresh overwrites it, and until then it is simply not
    today's.
    """
    if db is None:
        return ""
    try:
        if db.get_app_state(_SUMMARY_DATE_KEY) != today:
            return ""
        return str(db.get_app_state(_SUMMARY_KEY) or "")
    except Exception:
        return ""


def _snapshot(cfg) -> dict[str, Any]:
    try:
        return read_school_branch(cfg.db_path)
    except Exception as exc:
        debug_log(f"briefing could not read the School branch: {type(exc).__name__}", "webui")
        return {"branch": "school", "nodes": []}


@bp.route("")
def briefing() -> Response:
    """Today's items, the prose if it has been asked for, and the spoken gate."""
    cfg = _config()
    if cfg is None:
        return jsonify({
            "available": False, "items": [], "summary": "", "summary_date": None,
            "spoken": {"enabled": False, "time": "07:00", "last_delivered": None},
        })

    db = _db(cfg)
    today = _today(cfg)
    items = _items(_snapshot(cfg))
    try:
        return jsonify({
            "available": bool(items),
            "items": items,
            "summary": _cached_summary(db, today),
            "summary_date": today,
            "spoken": _spoken(cfg, db),
        })
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


@bp.route("/refresh", methods=["POST"])
def refresh() -> Response:
    """Generate today's prose through the same path the spoken briefing uses."""
    cfg = _config()
    if cfg is None:
        return jsonify(error="settings are not readable"), 503

    snapshot = _snapshot(cfg)
    items = _items(snapshot)
    if not items:
        # Nothing to summarise is not a failure, and it is certainly not a
        # reason to spend a CHAT-tier call producing a sentence about it.
        return jsonify(error="there is nothing in the School branch to summarise"), 409

    today = _today(cfg)
    from datetime import date as _date

    try:
        local_day = _date.fromisoformat(today)
    except ValueError:
        local_day = _date.today()

    summary = generate_morning_briefing(snapshot, cfg, local_day)
    if not summary:
        return jsonify(error="the model produced no briefing"), 503

    db = _db(cfg)
    if db is not None:
        try:
            db.set_app_state(_SUMMARY_KEY, summary)
            db.set_app_state(_SUMMARY_DATE_KEY, today)
        except Exception as exc:
            debug_log(f"briefing summary not cached: {type(exc).__name__}", "webui")
        finally:
            try:
                db.close()
            except Exception:
                pass

    debug_log("briefing summary generated from the control centre", "webui")
    return jsonify({
        "available": True,
        "items": items,
        "summary": summary,
        "summary_date": today,
        "spoken": _spoken(cfg, None),
    })
