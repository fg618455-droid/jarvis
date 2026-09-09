"""💬 The conversation-mode switch, reachable from outside the voice loop.

Conversation mode is a property of the running listener, but the control
that flips it lives elsewhere: the control centre, the tray, a hotkey. The
listener registers itself here on start, and callers reach it through these
functions without holding a reference to the listener or importing it.
"""

from __future__ import annotations

import threading
from typing import Protocol

from ..debug import debug_log


class ConversationController(Protocol):
    @property
    def is_conversation_active(self) -> bool: ...

    def start_conversation(self) -> None: ...

    def end_conversation(self) -> None: ...


_controller: ConversationController | None = None
_lock = threading.Lock()


def register_conversation_controller(controller: ConversationController | None) -> None:
    """Publish the running listener's switch, or clear it on shutdown."""
    global _controller
    with _lock:
        _controller = controller
    debug_log(
        f"conversation controller {'registered' if controller else 'cleared'}",
        "state",
    )


def conversation_mode_active() -> bool:
    """Whether a conversation is running. False when nothing is listening."""
    with _lock:
        controller = _controller
    if controller is None:
        return False
    try:
        return bool(controller.is_conversation_active)
    except Exception as exc:
        debug_log(f"conversation state unreadable: {exc}", "state")
        return False


def set_conversation_mode(enabled: bool) -> bool:
    """Turn conversation mode on or off.

    Returns whether the switch reached a listener. False means nothing was
    listening, so the caller can say so rather than report a state it did
    not actually set.
    """
    with _lock:
        controller = _controller
    if controller is None:
        debug_log("conversation mode requested with no listener registered", "state")
        return False
    try:
        if enabled:
            controller.start_conversation()
        else:
            controller.end_conversation()
    except Exception as exc:
        debug_log(f"conversation mode switch failed: {exc}", "state")
        return False
    return True
