"""Structured, opt-in package, file and Windows settings management."""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")
_FILE_OPERATIONS = frozenset({"listFiles", "readFile", "writeFile", "appendFile", "deleteFile"})
_POWER_PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "powerSaver": "a1841308-3541-4fab-bc81-f71556f20b4a",
    "highPerformance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
}
_PERSONALISE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_WINDOWS_DENIED_DIRECTORY_NAMES = (
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "Boot",
    "Recovery",
    "System Volume Information",
    "$Recycle.Bin",
)
_WINDOWS_DENIED_ROOT_FILE_NAMES = frozenset({
    "bootmgr", "bootnxt", "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "dumpstack.log.tmp", "ntldr", "ntdetect.com",
})
_POSIX_DENIED_ROOTS = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root",
    "/sbin", "/sys", "/usr", "/var", "/System", "/Library", "/Applications",
    "/private",
)


def _normalised_windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.abspath(value)).rstrip("\\/")


def _inside_or_equal(candidate: str, root: str, separator: str) -> bool:
    return candidate == root or candidate.startswith(root + separator)


def _is_hard_denied_path(value: str) -> bool:
    """Check deny roots lexically, before touching the target on disk."""
    if value.casefold().startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        return True
    unc_match = re.match(r"^\\\\[^\\]+\\([^\\]+)(?:\\|$)", value)
    if unc_match and unc_match.group(1).endswith("$"):
        return True
    windows_drive, _windows_tail = ntpath.splitdrive(value)
    if ntpath.isabs(value) and (windows_drive or value.startswith("\\")):
        candidate = _normalised_windows_path(value)
        drive, _tail = ntpath.splitdrive(candidate)
        if drive and not drive.startswith("\\"):
            for name in _WINDOWS_DENIED_DIRECTORY_NAMES:
                root = _normalised_windows_path(ntpath.join(drive + "\\", name))
                if _inside_or_equal(candidate, root, "\\"):
                    return True
            denied_root_files = {
                _normalised_windows_path(ntpath.join(drive + "\\", name))
                for name in _WINDOWS_DENIED_ROOT_FILE_NAMES
            }
            if candidate in denied_root_files:
                return True

        for environment_name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            configured = os.environ.get(environment_name)
            if configured:
                root = _normalised_windows_path(configured)
                if _inside_or_equal(candidate, root, "\\"):
                    return True
        return False

    if os.path.isabs(value):
        candidate = os.path.normcase(os.path.abspath(value)).rstrip(os.sep)
        for denied in _POSIX_DENIED_ROOTS:
            root = os.path.normcase(denied).rstrip(os.sep)
            if _inside_or_equal(candidate, root, os.sep):
                return True
    return False


def _has_ambiguous_windows_syntax(value: str) -> bool:
    """Reject Windows aliases whose spelling does not uniquely name a path."""
    drive, tail = ntpath.splitdrive(value)
    if not drive and not value.startswith("\\"):
        return False
    if ":" in tail:
        return True
    return any(part.endswith((" ", ".")) for part in re.split(r"[\\/]", tail) if part)


def _run_vector(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        shell=False,
    )


def _result_from_process(
    operation: str, process: subprocess.CompletedProcess[str]
) -> ToolExecutionResult:
    output = (process.stdout or process.stderr or "").strip()[:50_000]
    if process.returncode != 0:
        return ToolExecutionResult(
            success=False,
            reply_text=f"{operation} failed: {output or 'the operating system reported an error.'}",
        )
    return ToolExecutionResult(success=True, reply_text=output or f"{operation} completed.")


class SystemManagerTool(Tool):
    """Perform a fixed set of system-management actions without a shell."""

    def __init__(self, *, registry=None) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "systemManager"

    @property
    def description(self) -> str:
        return (
            "Inspect or manage this computer through fixed structured actions: list, install or "
            "uninstall exact winget package IDs; list, read, write, append or delete files at "
            "absolute paths outside the home folder except protected system paths; inspect or "
            "change Windows dark mode and the active balanced, power-saver or high-performance "
            "power plan. Use packageId from winget, never a display name."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "listInstalledPackages", "installPackage", "uninstallPackage",
                        "listFiles", "readFile", "writeFile", "appendFile", "deleteFile",
                        "getDarkMode", "setDarkMode", "getPowerPlan", "setPowerPlan",
                    ],
                },
                "packageId": {"type": "string", "description": "An exact winget package ID."},
                "path": {"type": "string", "description": "An absolute local or UNC path."},
                "content": {"type": "string"},
                "enabled": {"type": "boolean"},
                "powerPlan": {
                    "type": "string",
                    "enum": ["balanced", "powerSaver", "highPerformance"],
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        }

    def run(
        self, args: Optional[Dict[str, Any]], context: ToolContext
    ) -> ToolExecutionResult:
        if not isinstance(args, dict):
            return self._failure("systemManager requires a structured action object.")
        operation = args.get("operation")
        if not isinstance(operation, str) or operation not in self.inputSchema["properties"]["operation"]["enum"]:
            return self._failure("systemManager requires a supported operation.")

        try:
            if operation == "listInstalledPackages":
                return self._run_os_action(
                    operation,
                    ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
                )
            if operation in {"installPackage", "uninstallPackage"}:
                return self._manage_package(operation, args)
            if operation in _FILE_OPERATIONS:
                return self._manage_file(operation, args)
            if operation == "getDarkMode":
                return self._get_dark_mode()
            if operation == "setDarkMode":
                return self._set_dark_mode(args)
            if operation == "getPowerPlan":
                return self._run_os_action(operation, ["powercfg", "/getactivescheme"])
            if operation == "setPowerPlan":
                plan = args.get("powerPlan")
                if not isinstance(plan, str) or plan not in _POWER_PLANS:
                    return self._failure("setPowerPlan requires a supported powerPlan value.")
                return self._run_os_action(
                    operation, ["powercfg", "/setactive", _POWER_PLANS[plan]]
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failure(f"{operation} could not reach the operating system: {exc}")
        except Exception as exc:
            return self._failure(f"{operation} failed: {exc}")
        return self._failure("systemManager received an unsupported operation.")

    def _manage_package(self, operation: str, args: Dict[str, Any]) -> ToolExecutionResult:
        package_id = args.get("packageId")
        if not isinstance(package_id, str) or not _PACKAGE_ID_RE.fullmatch(package_id):
            return self._failure("A package action requires an exact winget packageId.")
        if operation == "installPackage":
            arguments = [
                "winget", "install", "--id", package_id, "--exact",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity",
            ]
        else:
            arguments = [
                "winget", "uninstall", "--id", package_id, "--exact",
                "--disable-interactivity",
            ]
        return self._run_os_action(operation, arguments)

    def _file_target(self, args: Dict[str, Any]) -> Path | ToolExecutionResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return self._failure("A file action requires an absolute path.")
        if raw_path != raw_path.strip() or _has_ambiguous_windows_syntax(raw_path):
            debug_log(f"systemManager refused ambiguous path syntax: {raw_path!r}", "security")
            return self._failure("The path uses ambiguous Windows syntax and is refused.")
        if not (ntpath.isabs(raw_path) or os.path.isabs(raw_path)):
            return self._failure("A file action requires an absolute path.")
        if _is_hard_denied_path(raw_path):
            debug_log(f"systemManager hard-deny refused path: {raw_path}", "security")
            return self._failure(f"Path is hard-denied for system management: {raw_path}")

        target = Path(raw_path).resolve(strict=False)
        if _is_hard_denied_path(str(target)):
            debug_log(f"systemManager hard-deny refused resolved path: {target}", "security")
            return self._failure(f"Path is hard-denied for system management: {target}")
        return target

    def _manage_file(self, operation: str, args: Dict[str, Any]) -> ToolExecutionResult:
        target = self._file_target(args)
        if isinstance(target, ToolExecutionResult):
            return target

        if operation == "listFiles":
            if not target.is_dir():
                return self._failure(f"Directory not found: {target}")
            entries = sorted(target.iterdir(), key=lambda item: item.name.casefold())[:100]
            lines = [f"{'DIR' if item.is_dir() else 'FILE'}: {item.name}" for item in entries]
            result = "\n".join(lines) if lines else f"No files found in {target}."
        elif operation == "readFile":
            if not target.is_file():
                return self._failure(f"File not found: {target}")
            content = target.read_text(encoding="utf-8", errors="replace")
            result = content[:50_000]
            if len(content) > 50_000:
                result += "\n... (truncated at 50,000 characters)"
        elif operation == "writeFile":
            content = args.get("content")
            if not isinstance(content, str):
                return self._failure("writeFile requires string content.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            result = f"Wrote {len(content)} characters to {target}."
        elif operation == "appendFile":
            content = args.get("content")
            if not isinstance(content, str):
                return self._failure("appendFile requires string content.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(content)
            result = f"Appended {len(content)} characters to {target}."
        else:
            if not target.is_file():
                return self._failure(f"File not found: {target}")
            target.unlink()
            result = f"Deleted file: {target}."

        debug_log(f"systemManager executed {operation} for {target}", "security")
        return ToolExecutionResult(success=True, reply_text=result)

    def _registry_api(self):
        if self._registry is not None:
            return self._registry
        if sys.platform != "win32":
            raise OSError("dark mode management is supported on Windows only")
        import winreg

        return winreg

    def _get_dark_mode(self) -> ToolExecutionResult:
        registry = self._registry_api()
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER, _PERSONALISE_KEY, 0, registry.KEY_READ
        ) as key:
            value, _value_type = registry.QueryValueEx(key, "AppsUseLightTheme")
        debug_log("systemManager executed getDarkMode", "security")
        return ToolExecutionResult(
            success=True, reply_text=f"Windows dark mode is {'on' if int(value) == 0 else 'off'}."
        )

    def _set_dark_mode(self, args: Dict[str, Any]) -> ToolExecutionResult:
        enabled = args.get("enabled")
        if not isinstance(enabled, bool):
            return self._failure("setDarkMode requires a boolean enabled value.")
        registry = self._registry_api()
        light_value = 0 if enabled else 1
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER, _PERSONALISE_KEY, 0, registry.KEY_SET_VALUE
        ) as key:
            registry.SetValueEx(
                key, "AppsUseLightTheme", 0, registry.REG_DWORD, light_value
            )
            registry.SetValueEx(
                key, "SystemUsesLightTheme", 0, registry.REG_DWORD, light_value
            )
        debug_log("systemManager executed setDarkMode", "security")
        return ToolExecutionResult(
            success=True, reply_text=f"Windows dark mode is {'on' if enabled else 'off'}."
        )

    def _run_os_action(self, operation: str, arguments: list[str]) -> ToolExecutionResult:
        process = _run_vector(arguments)
        result = _result_from_process(operation, process)
        if result.success:
            debug_log(f"systemManager executed {operation}", "security")
        return result

    @staticmethod
    def _failure(message: str) -> ToolExecutionResult:
        return ToolExecutionResult(success=False, reply_text=message)
