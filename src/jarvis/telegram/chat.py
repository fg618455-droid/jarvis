"""Telegram as a conversation channel.

A message here joins the same conversation as a spoken one and a typed one:
the same dialogue memory, the same tools, the same security gate. It is the
assistant reached from a phone instead of a microphone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jarvis.debug import debug_log


# Telegram refuses a message longer than this, so a long reply is split.
TELEGRAM_MAX_MESSAGE_CHARS = 4096

# The inbound cap the control centre's typed turns already use.
MAX_TEXT_CHARS = 4000


class TelegramChat:
    """Turn incoming messages into turns, and replies into messages."""

    def __init__(
        self,
        router,
        chat_id: str,
        *,
        submit: Callable[..., None] | None = None,
    ) -> None:
        self._router = router
        self._chat_id = str(chat_id or "").strip()
        self._submit = submit or _submit_to_daemon

    def handle_message(self, message: dict[str, Any]) -> None:
        """Handle one message. Called on the router's polling thread.

        Everything here is bounded and non-blocking: the submission is
        fire-and-forget, so the router stays free to deliver the security
        confirmation this very turn might raise.
        """
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not chat_id or chat_id != self._chat_id:
            debug_log("telegram chat message from an unauthorised chat ignored", "telegram")
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self._say("🤔 I can only read text messages.")
            return
        if len(text) > MAX_TEXT_CHARS:
            self._say(f"✂️ That message is too long. The limit is {MAX_TEXT_CHARS} characters.")
            return

        self._typing()
        debug_log("telegram turn submitted", "telegram")
        self._submit(
            text,
            on_complete=self._on_complete,
            on_busy=self._on_busy,
        )

    # ── outcomes ────────────────────────────────────────────────────────

    def _on_complete(self, reply: str | None) -> None:
        if reply:
            self._say(reply)
            return
        # A turn that produced nothing failed, was cancelled, or arrived while
        # the daemon was shutting down. Saying so beats silence.
        self._say("⚠️ That went wrong. Nothing came back from the assistant.")

    def _on_busy(self) -> None:
        self._say("⏳ Still working on the previous message. Send that again in a moment.")

    # ── sending ─────────────────────────────────────────────────────────

    def _say(self, text: str) -> None:
        for chunk in _split(text, TELEGRAM_MAX_MESSAGE_CHARS):
            try:
                self._router.send(
                    "sendMessage", {"chat_id": self._chat_id, "text": chunk}
                )
            except Exception as exc:
                debug_log(f"telegram reply could not be sent: {exc}", "telegram")
                return

    def _typing(self) -> None:
        try:
            self._router.send(
                "sendChatAction", {"chat_id": self._chat_id, "action": "typing"}
            )
        except Exception as exc:
            debug_log(f"telegram typing indicator failed: {exc}", "telegram")


def _split(text: str, limit: int) -> list[str]:
    """Cut a reply into pieces Telegram will accept, losing nothing."""
    if len(text) <= limit:
        return [text]
    return [text[start:start + limit] for start in range(0, len(text), limit)]


def _submit_to_daemon(text: str, *, on_complete=None, on_busy=None) -> None:
    """Hand the text to the daemon's shared reply entry point.

    Imported at call time because the daemon owns the conversation and may
    not be loaded when this module is.
    """
    from jarvis.daemon import submit_text_query

    submit_text_query(text, on_complete=on_complete, on_busy=on_busy)
