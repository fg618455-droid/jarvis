"""Debug logging utilities for Jarvis."""
import sys
import time
import threading
from collections import deque
from typing import Optional
from .config import load_settings
from .utils.redact import scrub_secrets


_last_check_time: float = 0.0
_cached_voice_debug: Optional[bool] = None
_CACHE_TTL_SECONDS: float = 2.0
_recent_logs: deque[dict[str, object]] = deque(maxlen=500)
_recent_logs_lock = threading.Lock()


def recent_logs(limit: int = 200) -> list[dict[str, object]]:
    """Return a bounded copy of the locally retained diagnostic entries."""
    with _recent_logs_lock:
        return list(_recent_logs)[-max(1, min(limit, 500)):]


def clear_recent_logs() -> None:
    """Clear the in-memory diagnostic log used by the Control Centre."""
    with _recent_logs_lock:
        _recent_logs.clear()


def _is_debug_enabled() -> bool:
    global _last_check_time, _cached_voice_debug
    now = time.time()
    if _cached_voice_debug is None or (now - _last_check_time) > _CACHE_TTL_SECONDS:
        try:
            _cached_voice_debug = bool(load_settings().voice_debug)
        except Exception:
            _cached_voice_debug = False
        _last_check_time = now
    return bool(_cached_voice_debug)


def debug_log(message: str, category: str = "debug") -> None:
    """Unified debug logging function for Jarvis.

    Args:
        message: The debug message to log
        category: The log category (e.g., "debug", "voice", "echo", "tts", etc.)
    """
    safe_message = scrub_secrets(str(message))
    with _recent_logs_lock:
        _recent_logs.append({
            "timestamp": time.time(),
            "category": str(category),
            "message": safe_message,
        })
    if not _is_debug_enabled():
        return
    try:
        print(f"[{category:^10}] {safe_message}", file=sys.stderr)
    except Exception:
        pass
