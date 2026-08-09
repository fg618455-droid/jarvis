"""Telegram confirmation channel using the Bot API."""

from __future__ import annotations

import html
import json
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import requests

from jarvis.debug import debug_log


class TelegramTransport(Protocol):
    def post(self, method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


class RequestsTelegramTransport:
    def __init__(self, bot_token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._session = requests.Session()

    def post(self, method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = self._session.post(
            f"{self._base_url}/{method}",
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram Bot API rejected the request")
        return data


class TelegramConfirm:
    """Send a button prompt and accept only the configured chat's decision."""

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        timeout_seconds: int = 60,
        *,
        transport: TelegramTransport | None = None,
        request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout = timeout_seconds
        self._transport = transport or (
            RequestsTelegramTransport(self.bot_token) if self.bot_token else None
        )
        self._request_id_factory = request_id_factory
        self._clock = clock

    @property
    def is_available(self) -> bool:
        return bool(self.bot_token and self.chat_id and self._transport)

    def ask(self, action_name: str, action_args: dict[str, Any]) -> bool:
        if not self.is_available:
            return False

        request_id = self._request_id_factory()
        args_text = json.dumps(action_args, ensure_ascii=False, indent=2, default=str)[:2500]
        text = (
            "🔐 <b>Jarvis security confirmation</b>\n\n"
            f"<b>Tool:</b> <code>{html.escape(action_name)}</code>\n"
            f"<b>Arguments:</b>\n<pre>{html.escape(args_text)}</pre>"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{request_id}"},
                {"text": "❌ Deny", "callback_data": f"deny:{request_id}"},
            ]]
        }
        self._transport.post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            timeout=min(15.0, float(self.timeout)),
        )
        debug_log(f"Telegram security confirmation sent for {action_name}", "security")

        deadline = self._clock() + self.timeout
        offset: int | None = None
        while self._clock() < deadline:
            remaining = max(0.1, deadline - self._clock())
            payload: dict[str, Any] = {
                "timeout": min(10, max(1, int(remaining))),
                "allowed_updates": ["callback_query"],
            }
            if offset is not None:
                payload["offset"] = offset
            data = self._transport.post(
                "getUpdates",
                payload,
                timeout=min(15.0, remaining + 2.0),
            )
            for update in data.get("result", []):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                callback = update.get("callback_query") or {}
                message = callback.get("message") or {}
                callback_chat_id = str((message.get("chat") or {}).get("id", ""))
                callback_data = callback.get("data")
                if callback_chat_id != self.chat_id:
                    continue
                if callback_data == f"approve:{request_id}":
                    self._edit_decision(message, "✅ Approved")
                    return True
                if callback_data == f"deny:{request_id}":
                    self._edit_decision(message, "❌ Denied")
                    return False

        debug_log(f"Telegram security confirmation timed out for {action_name}", "security")
        return False

    def _edit_decision(self, message: dict[str, Any], text: str) -> None:
        try:
            self._transport.post(
                "editMessageText",
                {
                    "chat_id": self.chat_id,
                    "message_id": message.get("message_id"),
                    "text": text,
                },
                timeout=10.0,
            )
        except Exception as exc:
            debug_log(f"Telegram decision acknowledgement failed: {exc}", "security")
