"""Connecting MCP servers, from the interface rather than from a text editor.

An MCP server is a command line and an environment: the assistant launches
it, asks what tools it offers, and keeps the session alive. Everything about
that is already in ``config.mcps``, and until now the only way to add one was
to edit ``config.json`` by hand and restart.

This is `config.mcps` given the same treatment `/api/settings` gives the
registry fields, and for the same reason: both write the same file. Only
non-default values are stored, keys this endpoint does not own survive
untouched, and a credential is writable but never readable.

`mcps` cannot ride `/api/settings` itself. That endpoint refuses any key the
field registry does not describe, and the registry describes scalars and
lists of uniform objects. A map from a name the user invents to a launch
description with an arbitrary environment is neither, so it gets its own
door rather than a special case inside someone else's.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, request

from jarvis.config import _load_json, _save_json, resolve_config_path
from jarvis.debug import debug_log
from jarvis.tools.registry import get_cached_mcp_tools, is_mcp_cache_initialized


bp = Blueprint("mcp", __name__, url_prefix="/api/mcp")

MASK = "•" * 8

# What the editor renders, in the order it renders it. Sent with the reading
# so the page describes no field the server does not accept, the same way the
# LLM route editor is driven by the schema the route API hands it.
SERVER_FIELDS = [
    {"key": "name", "label": "Name", "type": "str",
     "description": "How the assistant refers to this server; also the prefix on its tools"},
    {"key": "command", "label": "Command", "type": "str",
     "description": "The executable that starts the server, e.g. npx or uvx"},
    {"key": "args", "label": "Arguments", "type": "list",
     "description": "One argument per line"},
    {"key": "env", "label": "Environment", "type": "env",
     "description": "Credentials and settings passed to the server process"},
    {"key": "timeout_sec", "label": "Timeout", "type": "float", "suffix": "s",
     "nullable": True, "min": 0.1, "max": 3600,
     "description": "How long one tool call may take before it is abandoned"},
    {"key": "idle_timeout_sec", "label": "Idle timeout", "type": "float", "suffix": "s",
     "nullable": True, "min": 1, "max": 86400,
     "description": "Shut the server down after this long unused. Leave empty for servers that own something, such as a browser"},
]

# Everything the runtime reads off a server entry. A key outside this set is
# something a future release or a hand edit put there, and it is carried
# through untouched rather than dropped by an editor that does not know it.
KNOWN_KEYS = {"command", "args", "env", "timeout_sec", "idle_timeout_sec"}


def _mask(value: Any) -> str:
    """Show that a credential is set, and its last four, and no more."""
    text = str(value or "")
    if not text:
        return ""
    return MASK if len(text) <= 4 else f"{MASK}{text[-4:]}"


def _configured() -> dict[str, dict]:
    config = _load_json(resolve_config_path()) or {}
    servers = config.get("mcps")
    return servers if isinstance(servers, dict) else {}


def _connected_tools() -> dict[str, int]:
    """How many tools each server actually offered, from the live cache."""
    if not is_mcp_cache_initialized():
        return {}
    counts: dict[str, int] = {}
    for name in get_cached_mcp_tools():
        server = name.split("__", 1)[0] if "__" in name else None
        if server:
            counts[server] = counts.get(server, 0) + 1
    return counts


def _reading(index: int, name: str, entry: dict, tool_counts: dict[str, int]) -> dict:
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    served = tool_counts.get(name, 0)
    return {
        # A stable handle on the stored entry, so a rename is an edit rather
        # than a new server whose credentials were left behind under the old
        # name. The LLM route editor uses the same trick for the same reason.
        "_index": index,
        "name": name,
        "command": str(entry.get("command", "")),
        "args": [str(arg) for arg in (entry.get("args") or [])],
        "env": {key: _mask(value) for key, value in env.items()},
        "timeout_sec": entry.get("timeout_sec"),
        "idle_timeout_sec": entry.get("idle_timeout_sec"),
        "tool_count": served,
        "connected": bool(served),
    }


@bp.route("/servers")
def servers() -> Response:
    """Every configured server, how it launches, and whether it answered."""
    tool_counts = _connected_tools()
    configured = _configured()
    return jsonify({
        "servers": [
            _reading(index, name, entry if isinstance(entry, dict) else {}, tool_counts)
            for index, (name, entry) in enumerate(configured.items())
        ],
        "server_fields": SERVER_FIELDS,
        # Without a discovery pass nothing has been asked yet, so "not
        # connected" would be a guess rather than a reading.
        "discovered": is_mcp_cache_initialized(),
    })


def _original(index: Any, configured: dict[str, dict]) -> dict:
    """The stored entry a submitted server came from, by its stable index."""
    if not isinstance(index, int) or isinstance(index, bool):
        return {}
    entries = list(configured.values())
    if 0 <= index < len(entries):
        entry = entries[index]
        return entry if isinstance(entry, dict) else {}
    return {}


def _number(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None


def _entry(submitted: dict, configured: dict[str, dict]) -> tuple[str, dict]:
    """One submitted server as the shape config.json stores."""
    name = str(submitted.get("name", "")).strip()
    if not name:
        raise ValueError("every server needs a name")

    command = str(submitted.get("command", "")).strip()
    if not command:
        raise ValueError(f"{name} needs a command to run")

    raw_args = submitted.get("args") or []
    if isinstance(raw_args, str):
        args = [line.strip() for line in raw_args.splitlines() if line.strip()]
    elif isinstance(raw_args, list):
        args = [str(arg) for arg in raw_args]
    else:
        raise ValueError(f"{name}: arguments must be a list")

    previous = _original(submitted.get("_index"), configured)
    previous_env = previous.get("env") if isinstance(previous.get("env"), dict) else {}

    submitted_env = submitted.get("env") or {}
    if not isinstance(submitted_env, dict):
        raise ValueError(f"{name}: environment must be an object")

    env: dict[str, str] = {}
    for key, value in submitted_env.items():
        text = str(value if value is not None else "")
        # The page sent the mask back untouched, so the stored value stands.
        # Without this, opening the editor and pressing save would replace
        # every credential with eight bullets and its own last four.
        env[str(key)] = str(previous_env.get(key, "")) if text.startswith(MASK) else text

    # A key the editor does not describe was put there by a hand edit or by a
    # later release, and this endpoint is not the place it stops existing.
    entry: dict[str, Any] = {
        key: value for key, value in previous.items() if key not in KNOWN_KEYS
    }
    entry["command"] = command
    if args:
        entry["args"] = args
    if env:
        entry["env"] = env

    timeout = _number(submitted.get("timeout_sec"), f"{name}: timeout")
    if timeout is not None:
        entry["timeout_sec"] = timeout
    idle = _number(submitted.get("idle_timeout_sec"), f"{name}: idle timeout")
    if idle is not None:
        entry["idle_timeout_sec"] = idle

    return name, entry


@bp.route("/servers", methods=["PUT"])
def save() -> Response:
    """Replace the configured servers, preserving unchanged credentials."""
    payload = request.get_json(silent=True) or {}
    submitted = payload.get("servers")
    if not isinstance(submitted, list):
        return jsonify(error="servers must be a list"), 400

    configured = _configured()

    # Everything is validated before anything is written: a refusal half way
    # down the list would otherwise leave the file describing a set of
    # servers nobody asked for.
    built: dict[str, dict] = {}
    for item in submitted:
        if not isinstance(item, dict):
            return jsonify(error="every server must be an object"), 400
        try:
            name, entry = _entry(item, configured)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if name in built:
            return jsonify(error=f"two servers are both called {name}"), 400
        built[name] = entry

    config = _load_json(resolve_config_path()) or {}
    if built:
        config["mcps"] = built
    else:
        # An empty map is the default, and a default is not written.
        config.pop("mcps", None)

    if not _save_json(resolve_config_path(), config):
        return jsonify(error=f"could not write {resolve_config_path()}"), 500

    debug_log(
        f"MCP servers written from the control centre: {', '.join(built) or 'none'}",
        "webui",
    )
    # Connecting a server means launching a subprocess the running daemon
    # built its tool registry from, so the change is on disk now and in the
    # assistant after a restart. The page says so rather than implying the
    # new server is already reachable.
    return jsonify({"servers": sorted(built), "restart_required": True})
