"""Spoken or interactive-console confirmation using a numeric challenge."""

from __future__ import annotations

import secrets
import sys
import threading
import unicodedata
from collections.abc import Callable
from typing import Any

from jarvis.debug import debug_log


VoiceRequester = Callable[[str, dict[str, Any], str, int], str | None]
_voice_requester: VoiceRequester | None = None


def set_voice_confirmation_requester(requester: VoiceRequester | None) -> None:
    """Register the active listener's synchronous confirmation capture."""
    global _voice_requester
    _voice_requester = requester


def _new_challenge() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(4))


def _normalise_decimal_digits(value: str | None) -> str:
    digits: list[str] = []
    for character in value or "":
        try:
            digits.append(str(unicodedata.decimal(character)))
        except (TypeError, ValueError):
            continue
    return "".join(digits)


class VoiceConsoleConfirm:
    """Require the user to repeat or enter an unpredictable numeric code."""

    def __init__(
        self,
        timeout_seconds: int = 60,
        *,
        voice_requester: VoiceRequester | None = None,
        challenge_factory: Callable[[], str] = _new_challenge,
        input_reader: Callable[[str], str] = input,
    ) -> None:
        self.timeout = timeout_seconds
        self._requester = voice_requester
        self._challenge_factory = challenge_factory
        self._input_reader = input_reader

    @property
    def is_available(self) -> bool:
        requester = self._requester or _voice_requester
        return requester is not None or bool(getattr(sys.stdin, "isatty", lambda: False)())

    def ask(self, action_name: str, action_args: dict[str, Any]) -> bool:
        challenge = self._challenge_factory()
        requester = self._requester or _voice_requester
        print(f"🔐 Security confirmation required for {action_name}.", flush=True)
        print(f"    🔢 Repeat or enter code: {' '.join(challenge)}", flush=True)

        try:
            if requester is not None:
                response = requester(action_name, action_args, challenge, self.timeout)
            else:
                completed = threading.Event()
                captured: dict[str, str | None] = {"response": None}

                def _read_console() -> None:
                    try:
                        captured["response"] = self._input_reader("    🔐 Confirmation code: ")
                    finally:
                        completed.set()

                threading.Thread(
                    target=_read_console,
                    daemon=True,
                    name="JarvisSecurityConsole",
                ).start()
                completed.wait(self.timeout)
                response = captured["response"]
        except Exception as exc:
            debug_log(f"voice confirmation capture failed: {exc}", "security")
            raise

        approved = _normalise_decimal_digits(response) == challenge
        debug_log(f"voice confirmation {'approved' if approved else 'denied'}", "security")
        return approved
