"""Desktop confirmation channel with direct Qt and subprocess IPC adapters."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from typing import Any

from jarvis.debug import debug_log


SECURITY_IPC_PREFIX = "__SECURITY_CONFIRM__:"
DesktopRequester = Callable[[str, dict[str, Any], int], bool]
_desktop_requester: DesktopRequester | None = None
_pending_lock = threading.Lock()
_pending: dict[str, tuple[threading.Event, dict[str, bool]]] = {}


def set_desktop_confirmation_requester(requester: DesktopRequester | None) -> None:
    global _desktop_requester
    _desktop_requester = requester


def resolve_desktop_confirmation(request_id: str, approved: bool) -> bool:
    with _pending_lock:
        pending = _pending.get(request_id)
    if pending is None:
        return False
    event, result = pending
    result["approved"] = bool(approved)
    event.set()
    return True


class DesktopConfirm:
    """Request a Qt decision directly or through the desktop app's IPC bridge."""

    def __init__(
        self,
        timeout_seconds: int = 60,
        *,
        requester: DesktopRequester | None = None,
    ) -> None:
        self.timeout = timeout_seconds
        self._requester = requester

    @property
    def is_available(self) -> bool:
        return bool(self._requester or _desktop_requester or os.environ.get("JARVIS_DESKTOP_APP") == "1")

    def ask(self, action_name: str, action_args: dict[str, Any]) -> bool:
        requester = self._requester or _desktop_requester
        if requester is not None:
            return bool(requester(action_name, action_args, self.timeout))
        if os.environ.get("JARVIS_DESKTOP_APP") != "1":
            return False

        request_id = uuid.uuid4().hex
        event = threading.Event()
        result = {"approved": False}
        with _pending_lock:
            _pending[request_id] = (event, result)
        try:
            payload = {
                "request_id": request_id,
                "action_name": action_name,
                "action_args": action_args,
                "timeout_seconds": self.timeout,
            }
            print(f"{SECURITY_IPC_PREFIX}{json.dumps(payload, ensure_ascii=False, default=str)}", flush=True)
            if not event.wait(self.timeout):
                debug_log(f"desktop confirmation timed out for {action_name}", "security")
                return False
            return result["approved"]
        finally:
            with _pending_lock:
                _pending.pop(request_id, None)
