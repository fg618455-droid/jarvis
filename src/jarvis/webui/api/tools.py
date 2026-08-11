"""The tool catalogue: what is available, from where, and how it behaved.

An MCP server that never connected shows up here as "no tools discovered"
rather than as one line in a log that scrolled past at start-up.
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.runtime import get_recorder
from jarvis.security.gate import SecurityGate
from jarvis.tools.registry import (
    BUILTIN_TOOLS,
    get_cached_mcp_tools,
    is_mcp_cache_initialized,
    refresh_mcp_tools,
)


bp = Blueprint("tools", __name__, url_prefix="/api")


def _last_use_by_tool() -> dict[str, dict]:
    """When each tool last ran, from the turn history."""
    last: dict[str, dict] = {}
    for turn in get_recorder().history():
        for call in turn.get("tools", []):
            last[call["name"]] = {
                "at": turn.get("started_at"),
                "duration_ms": call.get("duration_ms"),
                "ok": call.get("ok"),
                "error": call.get("error"),
            }
    return last


def _needs_confirmation(gate: SecurityGate | None, name: str) -> bool | None:
    """Whether the gate would stop to ask before running this tool."""
    if gate is None:
        return None
    try:
        # An empty argument set is the honest question for a catalogue view:
        # localFiles only needs confirmation for the mutating operations, so
        # it answers "not always" rather than "never".
        return gate._requires_confirmation(name, {})
    except Exception:
        return None


@bp.route("/tools")
def tools() -> Response:
    """Every tool the assistant can reach, builtin and MCP."""
    cfg = load_settings()
    gate = SecurityGate.get_instance()
    last_use = _last_use_by_tool()

    entries = []
    for name, tool in BUILTIN_TOOLS.items():
        entries.append({
            "name": name,
            "origin": "builtin",
            "server": None,
            "description": getattr(tool, "description", ""),
            "needs_confirmation": _needs_confirmation(gate, name),
            "last_use": last_use.get(name),
        })

    mcp_tools = get_cached_mcp_tools() if is_mcp_cache_initialized() else {}
    for name, spec in mcp_tools.items():
        server = name.split("__", 1)[0] if "__" in name else None
        entries.append({
            "name": name,
            "origin": "mcp",
            "server": server,
            "description": getattr(spec, "description", ""),
            # Every MCP tool is protected, whatever the level.
            "needs_confirmation": True,
            "last_use": last_use.get(name),
        })

    servers = []
    for server_name, server_cfg in (getattr(cfg, "mcps", {}) or {}).items():
        served = [name for name in mcp_tools if name.startswith(f"{server_name}__")]
        servers.append({
            "name": server_name,
            "command": server_cfg.get("command", ""),
            "args": server_cfg.get("args", []),
            "timeout_sec": server_cfg.get("timeout_sec"),
            "idle_timeout_sec": server_cfg.get("idle_timeout_sec"),
            "tool_count": len(served),
            "connected": bool(served),
        })

    return jsonify({
        "tools": entries,
        "servers": servers,
        "mcp_discovered": is_mcp_cache_initialized(),
    })


@bp.route("/tools/refresh", methods=["POST"])
def refresh() -> Response:
    """Ask the MCP servers again what they offer."""
    try:
        discovered, errors = refresh_mcp_tools(verbose=False)
    except Exception as exc:
        debug_log(f"MCP refresh from the control centre failed: {exc}", "webui")
        return jsonify(error=str(exc)), 500
    return jsonify({"tool_count": len(discovered), "errors": errors})
