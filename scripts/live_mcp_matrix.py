"""Opt-in, secret-free MCP cold-start and harmless call acceptance.

This script never prints tool results, environment values, prompts, or nested
exception text. It is intentionally separate from normal CI because it starts
third-party programs and may trigger an OAuth browser flow.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from jarvis.tools.external.mcp_client import MCPClient, safe_mcp_error
from jarvis.tools.external.mcp_runtime import shutdown_runtime


SERVERS: dict[str, dict[str, Any]] = {
    "composio": {
        "transport": "stdio",
        "command": "npx",
        "args": [
            "-y", "mcp-remote@0.8.3", "https://connect.composio.dev/mcp",
            "--transport", "http-only",
        ],
        "timeout_sec": 180,
    },
    "youtube-jordan": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@jordanchoi/mcp-server-youtube-transcript@0.3.0"],
        "timeout_sec": 180,
    },
    "youtube-coya": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@coyasong/youtube-mcp-server@1.2.0"],
        "timeout_sec": 180,
    },
    "chrome": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@1.8.0"],
        "timeout_sec": 180,
    },
}

HARMLESS_CALLS: dict[str, tuple[str, dict[str, Any]]] = {
    "composio": ("COMPOSIO_GET_TOOL_SCHEMAS", {"tool_slugs": ["GMAIL_SEND_EMAIL"]}),
    "chrome": ("list_pages", {}),
    "youtube-coya": ("get-video-transcript", {"videoId": "dQw4w9WgXcQ"}),
}


def _record(name: str, *, call_tool: str = "", call_args: dict | None = None) -> dict:
    config = dict(SERVERS[name])
    config["env"] = {key: os.environ[key] for key in (
        "TEMP", "TMP", "npm_config_cache", "XDG_CACHE_HOME", "MCP_REMOTE_CONFIG_DIR"
    ) if key in os.environ}
    if name == "chrome" and os.environ.get("JARVIS_ACCEPTANCE_CHROME_PROFILE"):
        config["args"] = [*config["args"], "--user-data-dir=" + os.environ["JARVIS_ACCEPTANCE_CHROME_PROFILE"]]
    client = MCPClient({name: config})
    started = time.monotonic()
    try:
        tools = client.list_tools(name)
        result: dict[str, Any] = {
            "server": name,
            "status": "passed",
            "cold_start_ms": round((time.monotonic() - started) * 1000, 2),
            "tool_count": len(tools),
            "tool_names": sorted(str(tool.get("name", "")) for tool in tools),
            "call": None,
        }
        if call_tool:
            invoked = client.invoke_tool(name, call_tool, call_args or {})
            result["call"] = {
                "tool": call_tool,
                "status": "failed" if invoked.get("isError") else "passed",
            }
            if name == "youtube-coya" and call_tool == "get-video-transcript":
                transcript = str(invoked.get("text", "")).casefold()
                semantic = all(fragment in transcript for fragment in ("never gonna give", "never gonna let"))
                result["call"]["semantic_match"] = semantic
                if not semantic:
                    result["call"]["status"] = "failed"
            if result["call"]["status"] == "failed":
                result["status"] = "failed"
        return result
    except Exception as error:
        return {
            "server": name,
            "status": "failed",
            "cold_start_ms": round((time.monotonic() - started) * 1000, 2),
            "error": safe_mcp_error(error),
        }
    finally:
        shutdown_runtime()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server", choices=sorted(SERVERS))
    parser.add_argument("--call-tool", default="")
    parser.add_argument("--call-args", default="{}")
    parser.add_argument("--harmless-call", action="store_true")
    args = parser.parse_args()
    if os.environ.get("JARVIS_LIVE_ACCEPTANCE") != "1":
        parser.error("set JARVIS_LIVE_ACCEPTANCE=1 to start a third-party MCP server")
    try:
        call_args = json.loads(args.call_args)
        if not isinstance(call_args, dict):
            raise ValueError
    except ValueError:
        parser.error("--call-args must be a JSON object")
    call_tool = args.call_tool
    if args.harmless_call:
        call_tool, call_args = HARMLESS_CALLS.get(args.server, ("", {}))
        if not call_tool:
            parser.error("no built-in harmless call is defined for this server")
    result = _record(args.server, call_tool=call_tool, call_args=call_args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
