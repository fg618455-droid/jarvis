"""🔐 Approvals the user has already given, kept across restarts.

Answering the same dialog for every calendar entry is friction that teaches
the user to approve without reading, which costs more safety than the extra
question buys. This store lets one approval stand for a tool.

It buys that convenience with a real boundary. The key is the action name,
not the arguments, so approving `localFiles` for one deletion approves every
later deletion too. That is why the feature is off by default, why only an
approval is ever recorded, and why the file is a plain readable list the user
can delete.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ..debug import debug_log


def default_approvals_path() -> Path:
    override = os.environ.get("JARVIS_SECURITY_APPROVALS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jarvis" / "security_approvals.json"


class ApprovalStore:
    """The set of action names the user has approved at least once."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_approvals_path()
        self._lock = threading.RLock()
        self._approved = self._load()

    def _load(self) -> set[str]:
        """Read the file, treating anything unusable as an empty set.

        Fail closed: a file we cannot read must never be taken as blanket
        approval, because the whole point of the gate is that permission is
        something the user gave rather than something we assumed.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return set()
        if not isinstance(raw, dict):
            return set()
        approved = raw.get("approved")
        if not isinstance(approved, list):
            return set()
        return {name for name in approved if isinstance(name, str) and name}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    json.dump(
                        {"version": 1, "approved": sorted(self._approved)},
                        fh,
                        indent=2,
                    )
                try:
                    temp_path.chmod(0o600)
                except OSError:
                    pass
                os.replace(temp_path, self.path)
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
        except OSError as exc:
            # A remembered approval that cannot be written costs one extra
            # question next time, which is the safe direction to fail in.
            debug_log(f"security approvals could not be stored: {exc}", "security")

    def is_approved(self, action_name: str) -> bool:
        with self._lock:
            return action_name in self._approved

    def remember(self, action_name: str) -> None:
        with self._lock:
            if action_name in self._approved:
                return
            self._approved.add(action_name)
            self._save()
        debug_log(f"security approval remembered: {action_name}", "security")

    def forget(self, action_name: str) -> None:
        with self._lock:
            if action_name not in self._approved:
                return
            self._approved.discard(action_name)
            self._save()
        debug_log(f"security approval forgotten: {action_name}", "security")

    def forget_all(self) -> None:
        with self._lock:
            if not self._approved:
                return
            self._approved.clear()
            self._save()
        debug_log("all security approvals forgotten", "security")

    def approved_names(self) -> list[str]:
        with self._lock:
            return sorted(self._approved)
