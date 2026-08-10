"""Confirmation channel served by the control centre.

The desktop channel needs the Qt tray app, the Telegram channel needs a bot
token, and the voice channel needs speakers and a microphone free. A setup
that runs the daemon on its own and watches it in a browser has none of
those, and the gate then refuses everything because no channel answered.

This channel raises a card in the control centre and waits for the button.
It is available whenever the control centre is serving, and no more: a
pending request nobody can see is worse than no channel at all.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from jarvis.debug import debug_log


DECISION_LOG_SIZE = 50


@dataclass
class PendingConfirmation:
    """A tool waiting on a decision."""

    request_id: str
    action_name: str
    action_args: dict[str, Any]
    requested_at: float
    expires_at: float
    _decided: threading.Event = field(default_factory=threading.Event, repr=False)
    approved: bool = False

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action_name": self.action_name,
            "action_args": self.action_args,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "seconds_left": max(0.0, self.expires_at - time.time()),
        }


class WebConfirmations:
    """The pending requests and the decisions already taken."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}
        self._log: deque[dict] = deque(maxlen=DECISION_LOG_SIZE)
        self._lock = threading.Lock()
        self._serving = False

    # ── availability ────────────────────────────────────────────────────

    def set_serving(self, serving: bool) -> None:
        """Whether a control centre is up to show a request."""
        self._serving = bool(serving)

    @property
    def is_serving(self) -> bool:
        return self._serving

    # ── asking ──────────────────────────────────────────────────────────

    def ask(self, action_name: str, action_args: dict[str, Any], timeout: int) -> bool:
        request = PendingConfirmation(
            request_id=uuid.uuid4().hex,
            action_name=action_name,
            action_args=action_args or {},
            requested_at=time.time(),
            expires_at=time.time() + timeout,
        )
        with self._lock:
            self._pending[request.request_id] = request

        from jarvis.runtime import get_event_bus

        get_event_bus().publish("confirmation", request.to_dict())

        decided = request._decided.wait(timeout)
        with self._lock:
            self._pending.pop(request.request_id, None)

        outcome = "approved" if (decided and request.approved) else (
            "denied" if decided else "timed out"
        )
        self._record(action_name, action_args, outcome)
        debug_log(f"web confirmation {outcome}: {action_name}", "security")
        return bool(decided and request.approved)

    # ── deciding ────────────────────────────────────────────────────────

    def decide(self, request_id: str, approved: bool) -> bool:
        """Answer a pending request. False when there is nothing to answer."""
        with self._lock:
            request = self._pending.get(request_id)
        if request is None:
            return False
        request.approved = bool(approved)
        request._decided.set()

        from jarvis.runtime import get_event_bus

        get_event_bus().publish("confirmation_resolved", {
            "request_id": request_id,
            "approved": bool(approved),
        })
        return True

    # ── reading ─────────────────────────────────────────────────────────

    def pending(self) -> list[dict]:
        now = time.time()
        with self._lock:
            requests = list(self._pending.values())
        return [r.to_dict() for r in requests if r.expires_at > now]

    def decisions(self) -> list[dict]:
        with self._lock:
            return list(self._log)

    def _record(self, action_name: str, action_args: dict, outcome: str) -> None:
        with self._lock:
            self._log.append({
                "action_name": action_name,
                "action_args": action_args,
                "outcome": outcome,
                "at": time.time(),
            })

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._log.clear()
        self._serving = False


_confirmations = WebConfirmations()


def get_web_confirmations() -> WebConfirmations:
    """The process-wide register. One control centre, one queue."""
    return _confirmations


class WebConfirm:
    """The gate's view of this channel."""

    def __init__(self, timeout_seconds: int = 60) -> None:
        self.timeout = timeout_seconds

    @property
    def is_available(self) -> bool:
        return _confirmations.is_serving

    def ask(self, action_name: str, action_args: dict[str, Any]) -> bool:
        return _confirmations.ask(action_name, action_args, self.timeout)
