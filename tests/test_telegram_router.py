"""Behaviours of the single Telegram update router.

The Bot API confirms an update as soon as ``getUpdates`` is called with a
higher offset, so two pollers on one token destroy each other's updates.
These tests pin the behaviours that make one router the only poller.
"""

from __future__ import annotations

import threading
import time

import pytest

from jarvis.telegram.router import TelegramRouter


BOT_TOKEN = "test-token"
CHAT_ID = "4242"
OTHER_CHAT_ID = "9999"


def _message_update(
    update_id: int,
    text: str,
    chat_id: str = CHAT_ID,
    *,
    thread_id: int | None = None,
    from_username: str | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id,
        "chat": {"id": int(chat_id)},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    if from_username is not None:
        message["from"] = {"username": from_username}
    return {"update_id": update_id, "message": message}


def _callback_update(update_id: int, data: str, chat_id: str = CHAT_ID) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "data": data,
            "message": {"message_id": update_id, "chat": {"id": int(chat_id)}},
        },
    }


class FakeBotApi:
    """Enough of the Bot API to honour offset confirmation semantics."""

    def __init__(self, updates=None, *, idle_delay: float = 0.0) -> None:
        self.queue = list(updates or [])
        self.sent: list[tuple[str, dict]] = []
        self.fail_next = 0
        self.idle_delay = idle_delay
        self._lock = threading.Lock()

    def post(self, method: str, payload: dict, timeout: float) -> dict:
        if method == "getUpdates":
            return {"ok": True, "result": self._get_updates(payload)}
        with self._lock:
            self.sent.append((method, payload))
        return {"ok": True, "result": {"message_id": len(self.sent)}}

    def add(self, update: dict) -> None:
        with self._lock:
            self.queue.append(update)

    def payloads(self, method: str) -> list[dict]:
        with self._lock:
            return [payload for name, payload in self.sent if name == method]

    def _get_updates(self, payload: dict) -> list[dict]:
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise RuntimeError("transport blew up")
            offset = payload.get("offset")
            if offset == -1:
                return self.queue[-1:]
            if offset is not None:
                self.queue = [u for u in self.queue if u["update_id"] >= offset]
            batch = list(self.queue)
        if not batch and self.idle_delay:
            time.sleep(self.idle_delay)
        return batch


def _router(api: FakeBotApi, **kwargs) -> TelegramRouter:
    return TelegramRouter(BOT_TOKEN, CHAT_ID, transport=api, **kwargs)


def test_backlog_is_discarded_at_start():
    """A command sent while Jarvis was off is not executed on startup."""
    api = FakeBotApi([_message_update(1, "delete everything")])
    seen: list[dict] = []
    router = _router(api)
    router.set_message_handler(seen.append)

    router.drop_backlog()
    router.poll_once()

    assert seen == []


def test_text_message_from_the_configured_chat_reaches_the_handler():
    api = FakeBotApi()
    seen: list[dict] = []
    router = _router(api)
    router.set_message_handler(seen.append)
    router.drop_backlog()

    api.add(_message_update(7, "what is the weather"))
    router.poll_once()

    assert [message["text"] for message in seen] == ["what is the weather"]


def test_message_from_an_unknown_chat_is_ignored():
    api = FakeBotApi()
    seen: list[dict] = []
    router = _router(api)
    router.set_message_handler(seen.append)
    router.drop_backlog()

    api.add(_message_update(3, "let me in", chat_id=OTHER_CHAT_ID))
    router.poll_once()

    assert seen == []


def test_offset_advances_past_ignored_updates():
    """An ignored update is confirmed away, not redelivered forever."""
    api = FakeBotApi()
    seen: list[dict] = []
    router = _router(api)
    router.set_message_handler(seen.append)
    router.drop_backlog()

    api.add(_message_update(4, "let me in", chat_id=OTHER_CHAT_ID))
    router.poll_once()
    router.poll_once()

    assert api.queue == []
    assert seen == []


def test_messages_are_ignored_when_no_handler_is_registered():
    """Chat switched off: messages do nothing, confirmations still resolve."""
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    pending = router.register_confirmation("req-1")

    api.add(_message_update(5, "do something"))
    api.add(_callback_update(6, "approve:req-1"))
    router.poll_once()

    assert pending.wait(1.0) is True


def test_transport_error_does_not_stop_the_loop():
    api = FakeBotApi()
    seen: list[dict] = []
    router = _router(api)
    router.set_message_handler(seen.append)
    router.drop_backlog()

    api.fail_next = 1
    router.poll_once()

    api.add(_message_update(8, "still listening"))
    router.poll_once()

    assert [message["text"] for message in seen] == ["still listening"]


def test_callback_query_resolves_the_pending_confirmation():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    pending = router.register_confirmation("req-approve")

    api.add(_callback_update(9, "approve:req-approve"))
    router.poll_once()

    assert pending.wait(1.0) is True


def test_denial_resolves_the_pending_confirmation_as_refused():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    pending = router.register_confirmation("req-deny")

    api.add(_callback_update(10, "deny:req-deny"))
    router.poll_once()

    assert pending.wait(1.0) is False


def test_callback_from_an_unknown_chat_cannot_decide():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    pending = router.register_confirmation("req-guarded")

    api.add(_callback_update(11, "approve:req-guarded", chat_id=OTHER_CHAT_ID))
    router.poll_once()

    assert pending.wait(0.2) is False


def test_router_keeps_polling_while_a_turn_is_in_flight():
    """The deadlock guard.

    A turn started from Telegram can itself raise a Telegram confirmation.
    That decision only ever arrives if handling a message never blocks the
    polling thread, so the handler must hand off exactly as the production
    fire-and-forget submission does.
    """
    api = FakeBotApi(idle_delay=0.01)
    router = _router(api, poll_timeout_sec=1)
    decided: dict[str, bool] = {}

    def handler(message: dict) -> None:
        def turn() -> None:
            pending = router.register_confirmation("req-in-turn")
            try:
                decided["approved"] = pending.wait(5.0)
            finally:
                router.discard_confirmation("req-in-turn")

        threading.Thread(target=turn, daemon=True).start()

    router.set_message_handler(handler)
    router.start()
    try:
        api.add(_message_update(1, "delete the file"))
        time.sleep(0.2)
        api.add(_callback_update(2, "approve:req-in-turn"))

        deadline = time.time() + 5.0
        while "approved" not in decided and time.time() < deadline:
            time.sleep(0.02)
    finally:
        router.stop()

    assert decided.get("approved") is True


def test_one_router_serves_every_construction_site():
    """A second poller on one token deletes updates the first has not read."""
    from jarvis.security.telegram_confirm import TelegramConfirm
    from jarvis.telegram.router import get_router_for, reset_router

    try:
        shared = get_router_for("tok", "123")

        assert get_router_for("tok", "123") is shared
        assert TelegramConfirm("tok", "123")._router is shared
    finally:
        reset_router()


def test_a_different_bot_gets_a_different_router():
    from jarvis.telegram.router import get_router_for, reset_router

    try:
        first = get_router_for("tok", "123")
        assert get_router_for("tok", "999") is not first
        assert get_router_for("other-tok", "123") is not first
    finally:
        reset_router()


def test_router_reports_unavailable_without_credentials():
    assert TelegramRouter("", "", transport=FakeBotApi()).is_available is False
    assert TelegramRouter(BOT_TOKEN, "", transport=FakeBotApi()).is_available is False
    assert TelegramRouter(BOT_TOKEN, CHAT_ID, transport=FakeBotApi()).is_available is True


# ── crew topic watching ────────────────────────────────────────────────

CREW_CHAT_ID = "-1004343606702"


def test_a_watched_topic_buffers_its_messages():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    router.watch_topic(CREW_CHAT_ID, 2)

    api.add(_message_update(20, "gesundheitscheck ok", CREW_CHAT_ID, thread_id=2, from_username="dev_bot"))
    router.poll_once()

    messages = router.get_topic_messages(CREW_CHAT_ID, 2)
    assert [m["text"] for m in messages] == ["gesundheitscheck ok"]
    assert messages[0]["from"] == "dev_bot"


def test_an_unwatched_topic_is_not_buffered():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    # Nothing is watched.

    api.add(_message_update(21, "should be dropped", CREW_CHAT_ID, thread_id=2))
    router.poll_once()

    assert router.get_topic_messages(CREW_CHAT_ID, 2) == []


def test_a_different_thread_in_the_same_chat_is_not_buffered():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    router.watch_topic(CREW_CHAT_ID, 2)

    api.add(_message_update(22, "wrong topic", CREW_CHAT_ID, thread_id=5))
    router.poll_once()

    assert router.get_topic_messages(CREW_CHAT_ID, 2) == []
    assert router.get_topic_messages(CREW_CHAT_ID, 5) == []


def test_the_general_topic_has_no_thread_id():
    """AGENT_THREADS maps the "jarvis" agent's General topic to None."""
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    router.watch_topic(CREW_CHAT_ID, None)

    api.add(_message_update(23, "general topic reply", CREW_CHAT_ID))
    router.poll_once()

    assert [m["text"] for m in router.get_topic_messages(CREW_CHAT_ID, None)] == [
        "general topic reply"
    ]


def test_topic_buffer_keeps_only_the_most_recent_messages():
    api = FakeBotApi()
    router = _router(api)
    router.drop_backlog()
    router.watch_topic(CREW_CHAT_ID, 2)

    for i in range(15):
        api.add(_message_update(30 + i, f"msg {i}", CREW_CHAT_ID, thread_id=2))
    router.poll_once()

    messages = router.get_topic_messages(CREW_CHAT_ID, 2)
    assert len(messages) <= 10
    assert messages[-1]["text"] == "msg 14"


def test_watching_a_crew_topic_does_not_affect_chat_authorisation():
    """Crew buffering is additive: it never makes an unrelated chat "authorised"."""
    api = FakeBotApi()
    seen: list[dict] = []
    router = _router(api)
    router.set_message_handler(seen.append)
    router.drop_backlog()
    router.watch_topic(CREW_CHAT_ID, None)

    api.add(_message_update(24, "crew message", CREW_CHAT_ID))
    router.poll_once()

    # Buffered for checkCrewReplies...
    assert [m["text"] for m in router.get_topic_messages(CREW_CHAT_ID, None)] == [
        "crew message"
    ]
    # ...but never reaches the chat/confirmation handler, which only knows
    # about the router's own configured chat id.
    assert seen == []


def test_watch_topic_is_idempotent():
    api = FakeBotApi()
    router = _router(api)
    router.watch_topic(CREW_CHAT_ID, 2)
    router.watch_topic(CREW_CHAT_ID, 2)

    api.add(_message_update(25, "one", CREW_CHAT_ID, thread_id=2))
    router.poll_once()

    assert len(router.get_topic_messages(CREW_CHAT_ID, 2)) == 1
