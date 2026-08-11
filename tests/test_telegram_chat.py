"""Behaviours of Telegram as a conversation channel."""

from __future__ import annotations

import pytest

from jarvis.telegram.chat import TELEGRAM_MAX_MESSAGE_CHARS, TelegramChat
from jarvis.telegram.router import TelegramRouter

from test_telegram_router import BOT_TOKEN, CHAT_ID, OTHER_CHAT_ID, FakeBotApi


def _incoming(text: str, chat_id: str = CHAT_ID) -> dict:
    return {"message_id": 1, "chat": {"id": int(chat_id)}, "text": text}


def _chat(api: FakeBotApi, submit) -> TelegramChat:
    router = TelegramRouter(BOT_TOKEN, CHAT_ID, transport=api)
    return TelegramChat(router, CHAT_ID, submit=submit)


def _sent_texts(api: FakeBotApi) -> list[str]:
    return [payload.get("text", "") for payload in api.payloads("sendMessage")]


def test_reply_goes_back_to_the_configured_chat():
    api = FakeBotApi()

    def submit(text, *, on_complete=None, on_busy=None):
        on_complete("the weather is fine")

    _chat(api, submit).handle_message(_incoming("what is the weather"))

    assert "the weather is fine" in _sent_texts(api)
    assert all(
        str(payload["chat_id"]) == CHAT_ID for payload in api.payloads("sendMessage")
    )


def test_the_typed_text_is_what_reaches_the_reply_engine():
    api = FakeBotApi()
    submitted: list[str] = []

    def submit(text, *, on_complete=None, on_busy=None):
        submitted.append(text)
        on_complete("done")

    _chat(api, submit).handle_message(_incoming("log a coffee"))

    assert submitted == ["log a coffee"]


def test_a_busy_assistant_says_so_rather_than_dropping_the_message():
    api = FakeBotApi()

    def submit(text, *, on_complete=None, on_busy=None):
        on_busy()

    _chat(api, submit).handle_message(_incoming("are you there"))

    assert _sent_texts(api), "the user was told nothing about the rejected message"


def test_a_failed_turn_is_reported_honestly():
    api = FakeBotApi()

    def submit(text, *, on_complete=None, on_busy=None):
        on_complete(None)

    _chat(api, submit).handle_message(_incoming("what is the weather"))

    assert _sent_texts(api), "a failed turn left the user with no answer at all"


def test_a_long_reply_is_split_into_sendable_messages():
    api = FakeBotApi()
    long_reply = "x" * (TELEGRAM_MAX_MESSAGE_CHARS * 2 + 100)

    def submit(text, *, on_complete=None, on_busy=None):
        on_complete(long_reply)

    _chat(api, submit).handle_message(_incoming("tell me everything"))

    chunks = _sent_texts(api)
    assert len(chunks) >= 3
    assert all(len(chunk) <= TELEGRAM_MAX_MESSAGE_CHARS for chunk in chunks)
    assert "".join(chunks) == long_reply


def test_a_non_text_message_is_answered_without_running_a_turn():
    api = FakeBotApi()
    submitted: list[str] = []

    def submit(text, *, on_complete=None, on_busy=None):
        submitted.append(text)

    _chat(api, submit).handle_message({"message_id": 2, "chat": {"id": int(CHAT_ID)}})

    assert submitted == []
    assert _sent_texts(api), "a photo got no acknowledgement at all"


def test_an_overlong_message_is_refused_without_running_a_turn():
    api = FakeBotApi()
    submitted: list[str] = []

    def submit(text, *, on_complete=None, on_busy=None):
        submitted.append(text)

    _chat(api, submit).handle_message(_incoming("y" * 50_000))

    assert submitted == []
    assert _sent_texts(api)


def test_a_message_from_another_chat_never_runs_a_turn():
    api = FakeBotApi()
    submitted: list[str] = []

    def submit(text, *, on_complete=None, on_busy=None):
        submitted.append(text)

    _chat(api, submit).handle_message(_incoming("let me in", chat_id=OTHER_CHAT_ID))

    assert submitted == []


def test_conversation_is_off_until_it_is_switched_on(tmp_path, monkeypatch):
    """A message runs tools, so configuring a bot token must not grant that."""
    import json

    from jarvis.config import load_settings

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"telegram_bot_token": "tok", "telegram_chat_id": "123"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    assert load_settings().telegram_chat_enabled is False


def test_the_user_sees_that_the_assistant_is_working():
    api = FakeBotApi()

    def submit(text, *, on_complete=None, on_busy=None):
        on_complete("done")

    _chat(api, submit).handle_message(_incoming("think about this"))

    assert api.payloads("sendChatAction"), "no typing indicator was ever sent"
