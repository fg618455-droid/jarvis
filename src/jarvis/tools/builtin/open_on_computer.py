"""Open a website, a file, or an installed application on the user's computer.

Jarvis runs on the machine it is talking from, so "open YouTube" and "start
Notepad" are local actions rather than web lookups. Without this tool the
assistant can describe a link but never actually put it on screen.

Everything here is deliberately shell-free. URLs go through
:mod:`webbrowser`, applications are resolved to an absolute executable path
first and then started as a bare argument vector, and paths are handed to the
platform's own opener as a single argument. No user-supplied string is ever
concatenated into a command line, so a target carrying ``&&`` or ``|`` is a
name that will not resolve, not an instruction that will run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult

# Only schemes that hand the target to a browser are accepted. ``file:``,
# ``javascript:`` and friends turn "open this" into "read this off disk" or
# "run this", which is a different, far more privileged action.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# A target carrying an explicit scheme is a URL, whatever else it looks like.
_HAS_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Dotted targets are treated as bare domains once the application lookup has
# come up empty ("youtube.com", "www.bbc.co.uk/news"). Host labels are held to
# the characters a hostname may actually carry, so a Windows path that happens
# to contain a dot ("C:\\Users\\me\\notes.txt") is never mistaken for a site.
_DOMAIN_SHAPED_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#]\S*)?$"
)

# Suffixes that mark a dotted target as a program rather than a host, so a
# missing "notepad.exe" fails honestly instead of opening a browser at
# ``https://notepad.exe``. Technical file suffixes, not language patterns.
# ``.com`` is deliberately absent: as a top-level domain it outweighs the DOS
# executable format by every measure that matters here.
_EXECUTABLE_SUFFIXES = frozenset({
    "exe", "bat", "cmd", "msi", "lnk", "ps1",
    "app", "appimage", "desktop", "deb", "rpm",
})

# Where Windows records the location of installed programs that are not on
# PATH (Spotify, Chrome, Discord, …). Same table the Run dialog consults.
_WINDOWS_APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths"


def _home_root() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def _normalise_url(target: str) -> Optional[str]:
    """Return ``target`` as a browser-safe URL, or None when it is not one."""
    parsed = urlparse(target)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    return target


def _resolve_home_path(target: str) -> Optional[Path]:
    """Return an existing path inside the home directory, or None.

    Anything outside home is rejected the same way ``localFiles`` rejects it:
    opening a file launches whatever program is registered for its type, so
    the reachable set is kept to the user's own data.
    """
    expanded = os.path.expanduser(target)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = _home_root() / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    home = _home_root()
    if resolved != home and not str(resolved).startswith(str(home) + os.sep):
        debug_log(f"openOnComputer refused a path outside home: {resolved}", "tools")
        return None
    return resolved


def _resolve_windows_app_path(name: str) -> Optional[str]:
    """Look ``name`` up in the Windows App Paths registry table."""
    try:
        import winreg
    except ImportError:
        return None

    key_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, f"{_WINDOWS_APP_PATHS}\\{key_name}") as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            path = value.strip().strip('"')
            if os.path.isfile(path):
                return path
    return None


def _resolve_application(name: str) -> Optional[str]:
    """Return the absolute executable for an application name, or None.

    Only bare names are accepted: a target carrying a path separator is a
    path, and letting it through here would make every directory on the
    machine look like a launchable program.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        return None
    found = shutil.which(name)
    if found and os.path.isfile(found):
        return found
    if sys.platform == "win32":
        return _resolve_windows_app_path(name)
    return None


def _looks_like_bare_domain(target: str) -> bool:
    """Whether a scheme-less target should be read as a web address."""
    if not _DOMAIN_SHAPED_RE.match(target):
        return False
    host = target.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    suffix = host.rsplit(".", 1)[-1].lower()
    return suffix not in _EXECUTABLE_SUFFIXES


def _open_url(url: str) -> bool:
    """Hand a URL to the user's default browser."""
    return bool(webbrowser.open(url))


def _open_path(path: Path) -> None:
    """Hand a file or folder to the platform's own opener."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - single argument, no shell
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _launch_application(executable: str) -> None:
    """Start a resolved executable with no arguments and no shell."""
    if sys.platform == "darwin" and executable.endswith(".app"):
        subprocess.Popen(["open", "-a", executable])
        return
    subprocess.Popen([executable])


class OpenOnComputerTool(Tool):
    """Open a website, file, folder or application on the user's machine."""

    @property
    def name(self) -> str:
        return "openOnComputer"

    @property
    def description(self) -> str:
        return (
            "Open something on the user's own computer: a website in their browser, "
            "an installed application, or a file or folder in their home directory. "
            "Use this whenever the user asks to open, start, launch, play or show "
            "something rather than to be told about it. Websites need a full "
            "https:// address, so build the address for the site the user named "
            "(including a search or watch address when they asked for a specific "
            "thing on that site). Applications are named plainly, e.g. 'notepad'."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "A full https:// address, an application name such as "
                        "'notepad', or a path inside the home directory."
                    ),
                }
            },
            "required": ["target"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        target = str((args or {}).get("target") or "").strip()
        if not target:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_CONFIG,
                "openOnComputer needs a 'target': a full https:// address, an "
                "application name, or a path in the home directory.",
                phase="routing",
            )

        debug_log(f"openOnComputer resolving target: {target!r}", "tools")

        if _HAS_SCHEME_RE.match(target):
            url = _normalise_url(target)
            if url is None:
                debug_log(f"openOnComputer refused scheme in {target!r}", "tools")
                return ToolExecutionResult.failure(
                    ToolErrorCode.INVALID_CONFIG,
                    "Only http and https addresses can be opened.",
                    phase="routing",
                )
            return self._open_url_result(url, context)

        path = _resolve_home_path(target)
        if path is not None:
            try:
                _open_path(path)
            except OSError as exc:
                debug_log(f"openOnComputer failed to open path {path}: {exc}", "tools")
                return ToolExecutionResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    f"Could not open {path}.",
                    technical_details=str(exc),
                )
            debug_log(f"openOnComputer opened path: {path}", "tools")
            context.user_print(f"📂 Opened {path}")
            return ToolExecutionResult(success=True, reply_text=f"Opened {path} on this computer.")

        executable = _resolve_application(target)
        if executable is not None:
            try:
                _launch_application(executable)
            except OSError as exc:
                debug_log(f"openOnComputer failed to launch {executable}: {exc}", "tools")
                return ToolExecutionResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    f"Could not start {target}.",
                    technical_details=str(exc),
                )
            debug_log(f"openOnComputer launched application: {executable}", "tools")
            context.user_print(f"🚀 Started {target}")
            return ToolExecutionResult(
                success=True, reply_text=f"Started the application {target} on this computer."
            )

        if _looks_like_bare_domain(target):
            return self._open_url_result(f"https://{target}", context)

        debug_log(f"openOnComputer could not resolve {target!r}", "tools")
        return ToolExecutionResult.failure(
            ToolErrorCode.INVALID_ARGUMENT,
            f"Nothing on this computer matches {target!r}. Websites need a full "
            f"https:// address, applications need their installed name.",
            phase="execution",
        )

    def _open_url_result(self, url: str, context: ToolContext) -> ToolExecutionResult:
        try:
            opened = _open_url(url)
        except Exception as exc:
            debug_log(f"openOnComputer browser call failed for {url}: {exc}", "tools")
            return ToolExecutionResult.failure(
                ToolErrorCode.EXECUTION_FAILED,
                f"Could not open {url}.",
                technical_details=str(exc),
            )
        if not opened:
            debug_log(f"openOnComputer found no browser for {url}", "tools")
            return ToolExecutionResult.failure(
                ToolErrorCode.EXECUTION_FAILED,
                f"No browser was available to open {url}.",
            )
        debug_log(f"openOnComputer opened URL: {url}", "tools")
        context.user_print(f"🌐 Opened {url}")
        return ToolExecutionResult(success=True, reply_text=f"Opened {url} in the browser.")

