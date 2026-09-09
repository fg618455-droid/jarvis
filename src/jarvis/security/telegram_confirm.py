"""Telegram confirmation channel.

The prompt and its buttons are this module's business. Fetching the answer
is not: the router owns the one update stream the whole process shares, so
this channel registers what it is waiting for and blocks on an event until
the router hands it a decision.
"""

from __future__ import annotations

import html
import json
import uuid
from collections.abc import Callable
from typing import Any

from jarvis.config import DEFAULT_TELEGRAM_API_BASE_URL
from jarvis.debug import debug_log
from jarvis.telegram.router import TelegramRouter, get_router_for

MAX_ARGUMENT_CHARS = 2500


class TelegramConfirm:
    """Send a button prompt and accept only the configured chat's decision."""

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        timeout_seconds: int = 60,
        *,
        api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
        router: TelegramRouter | None = None,
        request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout = timeout_seconds
        # The shared router, never a private one: a second poller on this
        # token would delete the very updates this channel waits for.
        self._router = router or get_router_for(
            self.bot_token, self.chat_id, api_base_url
        )
        self._request_id_factory = request_id_factory

    @property
    def is_available(self) -> bool:
        return self._router.is_available

    def ask(self, action_name: str, action_args: dict[str, Any]) -> bool:
        if not self.is_available:
            return False

        request_id = self._request_id_factory()
        # Claimed before the prompt goes out, so an instant tap still lands.
        pending = self._router.register_confirmation(request_id)
        try:
            self._router.ensure_polling()
            response = self._router.send(
                "sendMessage", self._prompt(action_name, action_args, request_id)
            )
        except Exception as exc:
            self._router.discard_confirmation(request_id)
            debug_log(f"Telegram confirmation could not be sent: {exc}", "security")
            raise

        try:
            debug_log(f"Telegram security confirmation sent for {action_name}", "security")
            approved = pending.wait(self.timeout)
        finally:
            self._router.discard_confirmation(request_id)

        if not approved:
            debug_log(f"Telegram security confirmation not approved for {action_name}", "security")
        self._acknowledge(response, approved)
        return approved

    def _prompt(
        self, action_name: str, action_args: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        args_text = json.dumps(
            action_args, ensure_ascii=False, indent=2, default=str
        )[:MAX_ARGUMENT_CHARS]
        return {
            "chat_id": self.chat_id,
            "text": (
                "🔐 <b>Jarvis security confirmation</b>\n\n"
                f"<b>Tool:</b> <code>{html.escape(action_name)}</code>\n"
                f"<b>Arguments:</b>\n<pre>{html.escape(args_text)}</pre>"
            ),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"approve:{request_id}"},
                    {"text": "❌ Deny", "callback_data": f"deny:{request_id}"},
                ]]
            },
        }

    def _acknowledge(self, response: dict[str, Any] | None, approved: bool) -> None:
        """Say what was decided. A failure here changes no decision.

        The prompt itself is rewritten where possible, which takes its buttons
        away with it so a stale keyboard cannot invite a second tap.
        """
        text = "✅ Approved" if approved else "❌ Denied"
        message_id = ((response or {}).get("result") or {}).get("message_id")
        try:
            if message_id is None:
                self._router.send("sendMessage", {"chat_id": self.chat_id, "text": text})
            else:
                self._router.send(
                    "editMessageText",
                    {"chat_id": self.chat_id, "message_id": message_id, "text": text},
                )
        except Exception as exc:
            debug_log(f"Telegram decision acknowledgement failed: {exc}", "security")
