"""Console stream configuration shared by executable entry points."""

from __future__ import annotations

import sys


def force_utf8_console() -> None:
    """Reconfigure interpreter-owned console streams to UTF-8 in place."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        real_stream = getattr(sys, f"__{name}__", None)
        if stream is None or stream is not real_stream:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, TypeError):
            pass
