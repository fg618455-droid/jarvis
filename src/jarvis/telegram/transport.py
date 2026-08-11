"""Bot API calls against whichever server hosts the API."""

from __future__ import annotations

from typing import Any, Protocol

import requests

from jarvis.config import DEFAULT_TELEGRAM_API_BASE_URL


class TelegramTransport(Protocol):
    def post(self, method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


class RequestsTelegramTransport:
    """The Bot API over HTTPS.

    The Bot API server is published as software, so pointing this at a local
    instance keeps tool names, arguments and conversation on the user's own
    machine and off a third party's.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
    ) -> None:
        host = (base_url or DEFAULT_TELEGRAM_API_BASE_URL).strip().rstrip("/")
        self._base_url = f"{host}/bot{bot_token}"
        self._session = requests.Session()

    def endpoint(self, method: str) -> str:
        return f"{self._base_url}/{method}"

    def post(self, method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = self._session.post(
            self.endpoint(method),
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram Bot API rejected the request")
        return data
