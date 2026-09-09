"""Explicit application storage without changing HOME or authentication paths."""
from __future__ import annotations

import os
from pathlib import Path


def data_root_override() -> Path | None:
    value = os.environ.get("JARVIS_DATA_DIR", "").strip()
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("JARVIS_DATA_DIR must be an absolute path")
    return root


def data_directory() -> Path:
    return data_root_override() or Path.home() / ".local" / "share" / "jarvis"


def state_directory() -> Path:
    root = data_root_override()
    return root / "state" if root is not None else Path.home() / ".jarvis"
