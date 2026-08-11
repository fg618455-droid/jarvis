"""Reading and writing config.json through the shared field registry.

Same registry and same rules as the Qt settings window: only non-default
values are written, and keys the registry does not describe are left
exactly as they were. A credential is writable but never readable, so a
page left open does not put a bot token on screen.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, request

from jarvis.config import _load_json, _save_json, get_default_config, resolve_config_path
from jarvis.config_metadata import CATEGORIES, FIELD_METADATA, _is_default_value
from jarvis.debug import debug_log


bp = Blueprint("settings", __name__, url_prefix="/api")

SECRET_FIELD_TYPE = "password"
MASK = "•" * 8

# Changing these mid-run has no effect: Settings is frozen and the objects
# that read them are built once at start-up.
RESTART_REQUIRED_PREFIXES = (
    "whisper_", "vad_", "voice_", "sample_rate", "wake_", "tts_",
    "webui_", "dictation_", "llm_provider", "llm_routes", "ollama_", "embedding_",
)


def _mask(value: Any) -> str:
    """Show that a credential is set, and its last four, and no more."""
    text = str(value or "")
    if not text:
        return ""
    return MASK if len(text) <= 4 else f"{MASK}{text[-4:]}"


def _needs_restart(key: str) -> bool:
    return key.startswith(RESTART_REQUIRED_PREFIXES)


def _field_payload(meta, defaults: dict, config: dict) -> dict:
    value = config.get(meta.key, defaults.get(meta.key))
    is_secret = meta.field_type == SECRET_FIELD_TYPE
    return {
        "key": meta.key,
        "label": meta.label,
        "description": meta.description,
        "category": meta.category,
        "type": meta.field_type,
        "choices": [{"value": v, "label": label} for v, label in (meta.choices or [])] or None,
        "min": meta.min_val,
        "max": meta.max_val,
        "step": meta.step,
        "suffix": meta.suffix,
        "nullable": meta.nullable,
        "default": None if is_secret else defaults.get(meta.key),
        "value": _mask(value) if is_secret else value,
        "is_secret": is_secret,
        "is_set": bool(value) if is_secret else None,
        "is_default": _is_default_value(value, defaults.get(meta.key)),
        "restart_required": _needs_restart(meta.key),
    }


@bp.route("/settings")
def settings() -> Response:
    """Every editable field, its current value, and how to render it."""
    defaults = get_default_config()
    config = _load_json(resolve_config_path()) or {}

    return jsonify({
        "path": str(resolve_config_path()),
        "categories": [{"key": key, "label": label} for key, label in CATEGORIES],
        "fields": [_field_payload(meta, defaults, config) for meta in FIELD_METADATA],
    })


@bp.route("/settings", methods=["PUT"])
def save() -> Response:
    """Write changed fields, leaving everything else exactly as it was."""
    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return jsonify(error="changes must be an object of key to value"), 400

    known = {meta.key: meta for meta in FIELD_METADATA}
    unknown = sorted(set(changes) - set(known))
    if unknown:
        return jsonify(error=f"unknown settings: {', '.join(unknown)}"), 400

    defaults = get_default_config()
    config = _load_json(resolve_config_path()) or {}

    written: list[str] = []
    restart: list[str] = []
    for key, value in changes.items():
        meta = known[key]
        if meta.field_type == SECRET_FIELD_TYPE and isinstance(value, str) and value.startswith(MASK):
            # The page sent the mask back untouched, so the stored value stands.
            continue
        coerced = _coerce(meta, value)
        if _is_default_value(coerced, defaults.get(key)):
            config.pop(key, None)
        else:
            config[key] = coerced
        written.append(key)
        if _needs_restart(key):
            restart.append(key)

    if not _save_json(resolve_config_path(), config):
        return jsonify(error=f"could not write {resolve_config_path()}"), 500

    debug_log(f"settings written from the control centre: {', '.join(written)}", "webui")
    return jsonify({"written": written, "restart_required": restart})


def _coerce(meta, value: Any) -> Any:
    """Bring a value from JSON into the shape config.json expects."""
    if value is None:
        return None
    if meta.field_type == "bool":
        return bool(value)
    if meta.field_type == "int":
        if value == "":
            return None
        return int(_bounded(meta, float(value)))
    if meta.field_type == "float":
        if value == "":
            return None
        return float(_bounded(meta, float(value)))
    if meta.field_type == "list":
        if isinstance(value, list):
            return [str(item) for item in value]
        return [line.strip() for line in str(value).splitlines() if line.strip()]
    text = str(value).strip()
    return None if (meta.nullable and text == "") else text


def _bounded(meta, number: float) -> float:
    if meta.min_val is not None:
        number = max(float(meta.min_val), number)
    if meta.max_val is not None:
        number = min(float(meta.max_val), number)
    return number
