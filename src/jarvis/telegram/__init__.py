"""Telegram as a way in and out of the assistant.

One router owns the Bot API's update stream for the whole process, because
the API confirms updates by offset and a second poller would delete what the
first has not read yet. Confirmations and conversation both ride that router.
"""

from .chat import TelegramChat
from .router import TelegramRouter, get_router, get_router_for, reset_router
from .transport import RequestsTelegramTransport, TelegramTransport

__all__ = [
    "RequestsTelegramTransport",
    "TelegramChat",
    "TelegramRouter",
    "TelegramTransport",
    "get_router",
    "get_router_for",
    "reset_router",
]
