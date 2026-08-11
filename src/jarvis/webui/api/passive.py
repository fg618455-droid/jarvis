"""The text-only passive transcript record and its live switch."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from flask import Blueprint, Response, jsonify, request

from jarvis.config import (
    _load_json,
    _save_json,
    get_default_config,
    load_settings,
    resolve_config_path,
)
from jarvis.debug import debug_log
from jarvis.listening.passive_capture import (
    clear_passive_buffer,
    passive_capture_enabled,
    set_passive_capture_enabled,
)
from jarvis.memory.db import Database


bp = Blueprint("passive", __name__, url_prefix="/api")


@contextmanager
def _database() -> Iterator[Database]:
    cfg = load_settings()
    database = Database(cfg.db_path, cfg.sqlite_vss_path)
    try:
        yield database
    finally:
        database.close()


def _valid_date(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except (TypeError, ValueError):
        return False


@bp.route("/passive")
def list_record() -> Response:
    """List passive transcript lines with switch and digest state."""
    date_utc = (request.args.get("date") or "").strip()
    if date_utc and not _valid_date(date_utc):
        return jsonify(error="date must use YYYY-MM-DD"), 400
    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 1000))
    except (TypeError, ValueError):
        return jsonify(error="limit must be an integer"), 400

    cfg = load_settings()
    try:
        with _database() as database:
            lines = [
                dict(row)
                for row in database.list_passive_transcripts(
                    date_utc=date_utc or None,
                    limit=limit,
                )
            ]
            undigested = database.count_undigested_passive_transcripts()
    except Exception as exc:
        debug_log(f"passive record listing failed: {exc}", "webui")
        return jsonify(error="could not read the passive record"), 500

    return jsonify(
        {
            "lines": lines,
            "enabled": passive_capture_enabled(),
            "undigested_count": undigested,
            "llm_provider": cfg.llm_provider,
            "llm_base_url": cfg.llm_base_url,
        }
    )


@bp.route("/passive/enabled", methods=["POST"])
def set_enabled() -> Response:
    """Persist and apply the live passive-capture switch."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        return jsonify(error="enabled must be a boolean"), 400
    enabled = payload["enabled"]

    path = resolve_config_path()
    config = _load_json(path) or {}
    default = bool(get_default_config()["passive_capture_enabled"])
    if enabled == default:
        config.pop("passive_capture_enabled", None)
    else:
        config["passive_capture_enabled"] = enabled
    if not _save_json(path, config):
        return jsonify(error=f"could not write {path}"), 500

    set_passive_capture_enabled(enabled)
    debug_log(
        f"passive capture {'enabled' if enabled else 'disabled'} from the control centre",
        "webui",
    )
    return jsonify({"enabled": enabled})


@bp.route("/passive/<int:transcript_id>", methods=["DELETE"])
def delete_line(transcript_id: int) -> Response:
    """Delete one transcript line."""
    try:
        with _database() as database:
            deleted = database.delete_passive_transcript(transcript_id)
    except Exception as exc:
        debug_log(f"passive line deletion failed: {exc}", "webui")
        return jsonify(error="could not delete the passive transcript line"), 500
    if not deleted:
        return jsonify(error="passive transcript line not found"), 404
    return jsonify({"deleted": 1})


@bp.route("/passive", methods=["DELETE"])
def delete_record() -> Response:
    """Delete one UTC day or the whole passive transcript record."""
    delete_all = request.args.get("all") == "1"
    date_utc = (request.args.get("date") or "").strip()
    if delete_all == bool(date_utc):
        return jsonify(error="provide exactly one of all=1 or date=YYYY-MM-DD"), 400
    if date_utc and not _valid_date(date_utc):
        return jsonify(error="date must use YYYY-MM-DD"), 400

    try:
        with _database() as database:
            if delete_all:
                deleted = database.delete_all_passive_transcripts()
                clear_passive_buffer()
            else:
                deleted = database.delete_passive_transcripts_by_date(date_utc)
    except Exception as exc:
        debug_log(f"passive record deletion failed: {exc}", "webui")
        return jsonify(error="could not delete from the passive record"), 500
    debug_log(f"passive record deletion removed {deleted} lines", "webui")
    return jsonify({"deleted": deleted})
