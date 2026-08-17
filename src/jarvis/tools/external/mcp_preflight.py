"""Static validation performed before an MCP subprocess starts."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class MCPPreflightResult:
    available: bool
    code: str = "available"
    reason: str = ""


def _platform_allowed(config: Mapping[str, Any]) -> bool:
    platforms = config.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, (list, tuple, set)):
        return False
    current = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"
    return current in {str(value).strip().lower() for value in platforms}


def _has_unpinned_npx_package(config: Mapping[str, Any]) -> bool:
    if str(config.get("command", "")).lower() not in {"npx", "npx.cmd"}:
        return False
    args = [str(argument) for argument in config.get("args", [])]
    packages = [argument for argument in args if not argument.startswith("-")]
    return bool(packages and ("@latest" in packages[-1] or "@" not in packages[-1]))


def preflight_mcp_config(config: Mapping[str, Any]) -> MCPPreflightResult:
    """Validate configuration without executing third-party code."""
    if str(config.get("transport", "stdio")).lower() != "stdio":
        return MCPPreflightResult(False, "unsupported", "Only stdio MCP transport is supported.")
    if not _platform_allowed(config):
        return MCPPreflightResult(False, "unsupported", "This MCP server is not compatible with this operating system.")
    command = str(config.get("command", "")).strip()
    if not command:
        return MCPPreflightResult(False, "invalid_config", "MCP server command is missing.")
    if _has_unpinned_npx_package(config):
        return MCPPreflightResult(False, "invalid_config", "MCP npm packages must use an exact version, not @latest.")
    if os.path.isabs(command) and not os.path.isfile(command):
        return MCPPreflightResult(False, "unavailable", "MCP server executable does not exist.")
    return MCPPreflightResult(True)
