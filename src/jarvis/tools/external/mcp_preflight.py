"""Static validation performed before an MCP subprocess starts."""

from __future__ import annotations

import os
import ipaddress
import re
import socket
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


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


def _is_pinned_npm_specifier(specifier: str) -> bool:
    """Whether an npm package specifier names an exact version.

    A scoped package carries a leading ``@`` that is part of its name, so the
    version separator is the first ``@`` after that scope, not the first ``@``
    in the string.
    """
    body = specifier[1:] if specifier.startswith("@") else specifier
    _, separator, version = body.partition("@")
    if not separator:
        return False
    # npm tags (``latest``, ``next``), ranges and git references are mutable.
    # Permit only an exact semver, including an optional prerelease/build tag.
    return bool(re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        version.strip(),
    ))


def _has_unpinned_npx_package(config: Mapping[str, Any]) -> bool:
    if str(config.get("command", "")).lower() not in {"npx", "npx.cmd"}:
        return False
    args = [str(argument) for argument in config.get("args", [])]
    packages = [argument for argument in args if not argument.startswith("-")]
    if not packages:
        return False
    # The package specifier is the first non-flag argument. Everything after it
    # belongs to the server being launched, so a server that takes a URL or a
    # path must not be mistaken for an unpinned package.
    return not _is_pinned_npm_specifier(packages[0])


def _remote_endpoint(config: Mapping[str, Any]) -> str | None:
    """Return an mcp-remote URL without confusing it with the npm package."""
    if str(config.get("command", "")).lower() not in {"npx", "npx.cmd"}:
        return None
    positional = [str(value) for value in config.get("args", []) if not str(value).startswith("-")]
    if not positional or not positional[0].split("@", 1)[0].endswith("mcp-remote"):
        return None
    return next((value for value in positional[1:] if value.startswith(("https://", "http://"))), None)


def _endpoint_available(url: str) -> MCPPreflightResult:
    parsed = None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port or 443
    except ValueError:
        host = ""
    if (
        parsed is None or parsed.scheme != "https" or not host
        or parsed.username or parsed.password
    ):
        return MCPPreflightResult(False, "invalid_config", "Remote MCP endpoints must use HTTPS.")
    if host == "localhost" or host.endswith(".local"):
        return MCPPreflightResult(False, "invalid_config", "Remote MCP endpoints cannot use a local address.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        return MCPPreflightResult(False, "invalid_config", "Remote MCP endpoints cannot use a local address.")
    try:
        # Resolve before the request so an apparently public hostname cannot
        # use this server-side preflight as a path into a private network.
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return MCPPreflightResult(
                    False, "invalid_config",
                    "Remote MCP endpoints cannot resolve to a local address.",
                )
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Jarvis-MCP-Preflight/1"})
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=4):
            return MCPPreflightResult(True)
    except urllib.error.HTTPError as error:
        # Authentication and method errors still prove DNS/TLS/connectivity.
        if error.code in {400, 401, 403, 405, 406, 415}:
            return MCPPreflightResult(True)
        if 300 <= error.code < 400:
            return MCPPreflightResult(
                False, "endpoint_unreachable",
                "Remote MCP endpoint redirected during preflight.",
            )
        return MCPPreflightResult(False, "endpoint_unreachable", f"Remote MCP endpoint returned HTTP {error.code}.")
    except TimeoutError:
        return MCPPreflightResult(False, "endpoint_unreachable", "Remote MCP endpoint timed out.")
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            return MCPPreflightResult(False, "endpoint_unreachable", "Remote MCP endpoint timed out.")
        return MCPPreflightResult(False, "offline", "Remote MCP endpoint is not reachable.")
    except OSError:
        return MCPPreflightResult(False, "offline", "Remote MCP endpoint is not reachable.")


def preflight_mcp_config(config: Mapping[str, Any], *, live: bool = False) -> MCPPreflightResult:
    """Validate a server config; optionally verify its executable and endpoint."""
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
    if live and not os.path.isabs(command):
        lookup = "npx.cmd" if os.name == "nt" and command.lower() == "npx" else command
        if shutil.which(lookup) is None:
            return MCPPreflightResult(False, "executable_missing", "MCP server executable is not available on PATH.")
    endpoint = _remote_endpoint(config)
    if live and endpoint:
        return _endpoint_available(endpoint)
    return MCPPreflightResult(True)
