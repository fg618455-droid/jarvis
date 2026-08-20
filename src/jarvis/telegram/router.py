"""The single owner of the Telegram update stream.

The Bot API considers an update confirmed as soon as ``getUpdates`` is called
with an offset above its id, and ``allowed_updates`` does not filter updates
that already existed when the call was made. Two pollers on one bot token
therefore delete each other's updates: a message typed while a confirmation
is pending would simply never arrive. One router polls, and everything that
needs Telegram goes through it.

The polling thread must never wait on a reply. A turn started from Telegram
can raise a Telegram confirmation, and that decision only arrives if this
thread is still free to fetch it, so message handlers hand off their work.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from jarvis.config import DEFAULT_TELEGRAM_API_BASE_URL
from jarvis.debug import debug_log

from .transport import RequestsTelegramTransport, TelegramTransport


DEFAULT_POLL_TIMEOUT_SEC = 25
ERROR_BACKOFF_SEC = 3.0
SEND_TIMEOUT_SEC = 10.0
TOPIC_BUFFER_MAXLEN = 10

APPROVE_PREFIX = "approve:"
DENY_PREFIX = "deny:"


class PendingConfirmation:
    """A tool call waiting for a button in Telegram."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.approved = False
        self._decided = threading.Event()

    def decide(self, approved: bool) -> None:
        self.approved = bool(approved)
        self._decided.set()

    def wait(self, timeout: float) -> bool:
        """Block for a decision. A timeout is a refusal, never an approval."""
        decided = self._decided.wait(timeout)
        return bool(decided and self.approved)


class TelegramRouter:
    """Poll the Bot API once for everyone and dispatch what arrives."""

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        *,
        api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
        transport: TelegramTransport | None = None,
        poll_timeout_sec: int = DEFAULT_POLL_TIMEOUT_SEC,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.poll_timeout_sec = poll_timeout_sec
        self._transport = transport or (
            RequestsTelegramTransport(self.bot_token, base_url=api_base_url)
            if self.bot_token
            else None
        )
        self._offset: int | None = None
        self._message_handler: Callable[[dict[str, Any]], None] | None = None
        self._pending: dict[str, PendingConfirmation] = {}
        self._watched_topics: dict[tuple[str, int | None], deque] = {}
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fingerprint: tuple | None = None

    # ── availability and lifecycle ──────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return bool(self.bot_token and self.chat_id and self._transport)

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def set_message_handler(
        self, handler: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Register what receives conversation messages.

        Without one, messages are read off the stream and dropped, which is
        what a user who never switched the chat channel on expects.
        """
        self._message_handler = handler

    def start(self) -> None:
        """Begin polling. Starting an already-running router does nothing.

        Serialised, because a second live polling thread on one token is the
        precise failure this class exists to prevent.
        """
        with self._lifecycle_lock:
            if not self.is_available or self.is_running:
                return
            self._stop.clear()
            self.drop_backlog()
            self._thread = threading.Thread(
                target=self._run, name="jarvis-telegram-router", daemon=True
            )
            self._thread.start()
            debug_log("telegram router started", "telegram")

    def ensure_polling(self) -> None:
        """Start the router if nothing has started it yet.

        A confirmation can happen in a process with no daemon running, and it
        needs the update stream just as much as the chat channel does.
        """
        if not self.is_running:
            self.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=timeout)
                # A thread still inside a long poll is left owning the slot so
                # that nothing starts a second poller alongside it; it exits on
                # its own when the poll returns.
                if thread.is_alive():
                    debug_log("telegram router still finishing its poll", "telegram")
                    return
            self._thread = None
            debug_log("telegram router stopped", "telegram")

    def drop_backlog(self) -> None:
        """Skip whatever queued up while nobody was listening.

        An instruction sent last night should not run at breakfast because the
        daemon happened to come back up.
        """
        if not self.is_available:
            return
        try:
            data = self._transport.post(
                "getUpdates", {"offset": -1, "timeout": 0}, timeout=SEND_TIMEOUT_SEC
            )
        except Exception as exc:
            debug_log(f"telegram backlog check failed: {exc}", "telegram")
            return
        results = data.get("result") or []
        if results:
            last_id = results[-1].get("update_id")
            if isinstance(last_id, int):
                self._offset = last_id + 1
                debug_log(f"telegram backlog discarded up to {last_id}", "telegram")

    # ── sending ─────────────────────────────────────────────────────────

    def send(
        self, method: str, payload: dict[str, Any], timeout: float = SEND_TIMEOUT_SEC
    ) -> dict[str, Any]:
        if self._transport is None:
            raise RuntimeError("Telegram transport is not configured")
        return self._transport.post(method, payload, timeout=timeout)

    # ── confirmations ───────────────────────────────────────────────────

    def register_confirmation(self, request_id: str) -> PendingConfirmation:
        """Claim a request id before its prompt is sent.

        Registration comes first so a decision taken the instant the message
        lands still finds something to resolve.
        """
        pending = PendingConfirmation(request_id)
        with self._lock:
            self._pending[request_id] = pending
        return pending

    def discard_confirmation(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def resolve_confirmation(self, request_id: str, approved: bool) -> bool:
        """Answer a pending request. False when there is nothing to answer."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.decide(approved)
        return True

    def has_pending_confirmations(self) -> bool:
        with self._lock:
            return bool(self._pending)

    # ── crew topic watch ────────────────────────────────────────────────

    def watch_topic(self, chat_id: str, thread_id: int | None) -> None:
        """Start buffering messages from a chat/topic pair, idempotently.

        Additive and independent of ``self.chat_id``: it never changes what
        the confirmation and chat channel treat as authorised, it only opts
        one more scope into being remembered for later read-back.
        """
        key = (str(chat_id or "").strip(), thread_id)
        with self._lock:
            self._watched_topics.setdefault(key, deque(maxlen=TOPIC_BUFFER_MAXLEN))

    def get_topic_messages(self, chat_id: str, thread_id: int | None) -> list[dict[str, Any]]:
        """A snapshot of the most recent buffered messages, oldest first.

        Only ever reflects what arrived while this router was polling — the
        Bot API has no way to fetch what came before that.
        """
        key = (str(chat_id or "").strip(), thread_id)
        with self._lock:
            buffer = self._watched_topics.get(key)
            return list(buffer) if buffer is not None else []

    def _buffer_topic_message(self, message: dict[str, Any]) -> None:
        chat_id = str((message.get("chat") or {}).get("id", ""))
        thread_id = message.get("message_thread_id")
        key = (chat_id, thread_id)
        with self._lock:
            buffer = self._watched_topics.get(key)
            if buffer is None:
                return
            sender = message.get("from") or {}
            buffer.append({
                "from": sender.get("username") or sender.get("first_name") or "unknown",
                "text": message.get("text"),
                "date": message.get("date"),
            })

    # ── polling ─────────────────────────────────────────────────────────

    def poll_once(self) -> None:
        """Fetch one batch and dispatch it. The polling thread loops on this."""
        if not self.is_available:
            return
        payload: dict[str, Any] = {
            "timeout": self.poll_timeout_sec,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            payload["offset"] = self._offset
        try:
            data = self._transport.post(
                "getUpdates", payload, timeout=self.poll_timeout_sec + 5.0
            )
        except Exception as exc:
            debug_log(f"telegram poll failed: {exc}", "telegram")
            return

        for update in data.get("result") or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance past every update, including the ones this router
                # ignores, so an unauthorised sender cannot wedge the stream.
                self._offset = update_id + 1
            try:
                self._dispatch(update)
            except Exception as exc:
                debug_log(f"telegram dispatch failed: {exc}", "telegram")

    def _run(self) -> None:
        while not self._stop.is_set():
            before = time.monotonic()
            self.poll_once()
            if self._stop.is_set():
                break
            # A transport that fails instantly would otherwise spin the loop.
            if time.monotonic() - before < 0.05:
                self._stop.wait(ERROR_BACKOFF_SEC)

    def _dispatch(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._dispatch_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, dict):
            self._buffer_topic_message(message)
            self._dispatch_message(message)

    def _dispatch_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message") or {}
        if not self._is_authorised(message):
            debug_log("telegram callback from an unauthorised chat ignored", "telegram")
            return
        data = callback.get("data")
        if not isinstance(data, str):
            return
        if data.startswith(APPROVE_PREFIX):
            request_id, approved = data[len(APPROVE_PREFIX):], True
        elif data.startswith(DENY_PREFIX):
            request_id, approved = data[len(DENY_PREFIX):], False
        else:
            return
        if self.resolve_confirmation(request_id, approved):
            debug_log(
                f"telegram confirmation {'approved' if approved else 'denied'}",
                "telegram",
            )

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        if not self._is_authorised(message):
            debug_log("telegram message from an unauthorised chat ignored", "telegram")
            return
        handler = self._message_handler
        if handler is None:
            return
        debug_log("telegram message received", "telegram")
        handler(message)

    def _is_authorised(self, message: dict[str, Any]) -> bool:
        chat_id = str((message.get("chat") or {}).get("id", ""))
        return bool(chat_id) and chat_id == self.chat_id


_router: TelegramRouter | None = None
_router_lock = threading.Lock()


def get_router_for(
    bot_token: str | None,
    chat_id: str | None,
    base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
) -> TelegramRouter:
    """The one router for these credentials.

    Every construction site goes through here. Handing out a second router
    for the same bot token is the failure this module exists to prevent, so
    there is no way to ask for one.

    A changed token, chat or API host is a different bot, so the previous
    router is stopped and replaced rather than left polling.
    """
    global _router
    fingerprint = (
        (bot_token or "").strip(),
        str(chat_id or "").strip(),
        (base_url or DEFAULT_TELEGRAM_API_BASE_URL).strip().rstrip("/"),
    )

    with _router_lock:
        existing = _router
        if existing is not None and existing._fingerprint == fingerprint:
            return existing
        if existing is not None:
            existing.stop()
        router = TelegramRouter(
            fingerprint[0], fingerprint[1], api_base_url=fingerprint[2]
        )
        router._fingerprint = fingerprint
        _router = router
        return router


def get_router(cfg) -> TelegramRouter:
    """The process-wide router for the configured settings."""
    # ``load_settings`` has already applied the environment fallback, so the
    # settings carry the credentials this router should use.
    return get_router_for(
        getattr(cfg, "telegram_bot_token", ""),
        getattr(cfg, "telegram_chat_id", ""),
        getattr(cfg, "telegram_api_base_url", DEFAULT_TELEGRAM_API_BASE_URL),
    )


def reset_router() -> None:
    """Drop the process-wide router. Used when the daemon shuts down."""
    global _router
    with _router_lock:
        if _router is not None:
            _router.stop()
        _router = None
