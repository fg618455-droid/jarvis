"""Central confirmation policy for tool execution."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar, Protocol

from jarvis.debug import debug_log


LEVEL_OFF = "off"
LEVEL_CRITICAL = "critical"
LEVEL_PARANOID = "paranoid"
VALID_LEVELS = frozenset({LEVEL_OFF, LEVEL_CRITICAL, LEVEL_PARANOID})

_CRITICAL_BUILTINS = frozenset({"deleteMeal"})
_LOCAL_FILE_MUTATIONS = frozenset({"write", "append", "delete"})


class ConfirmationChannel(Protocol):
    @property
    def is_available(self) -> bool: ...

    def ask(self, action_name: str, action_args: dict[str, Any]) -> bool: ...


class SecurityGate:
    """Apply the configured policy before a valid tool is executed."""

    _instance: ClassVar[SecurityGate | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        level: str = LEVEL_CRITICAL,
        *,
        channels: Mapping[str, ConfirmationChannel] | None = None,
        confirm_channels: Iterable[str] | None = None,
    ) -> None:
        self.level = level if level in VALID_LEVELS else LEVEL_CRITICAL
        self._channels = dict(channels or {})
        self._confirm_channels = (
            list(confirm_channels)
            if confirm_channels is not None
            else ["desktop", "web", "telegram", "voice"]
        )
        self._fingerprint: tuple | None = None
        SecurityGate._instance = self
        debug_log(f"security gate initialised at {self.level} level", "security")

    @classmethod
    def from_settings(
        cls,
        cfg,
        *,
        channels: Mapping[str, ConfirmationChannel] | None = None,
    ) -> SecurityGate:
        """Build a gate and its configured channels from live settings."""
        if channels is None:
            from .desktop_confirm import DesktopConfirm
            from .telegram_confirm import TelegramConfirm
            from .voice_confirm import VoiceConsoleConfirm
            from .web_confirm import WebConfirm

            timeout = cfg.security_confirmation_timeout_sec
            channels = {
                "desktop": DesktopConfirm(timeout_seconds=timeout),
                "web": WebConfirm(timeout_seconds=timeout),
                "telegram": TelegramConfirm(
                    cfg.telegram_bot_token,
                    cfg.telegram_chat_id,
                    timeout_seconds=timeout,
                    api_base_url=cfg.telegram_api_base_url,
                ),
                "voice": VoiceConsoleConfirm(timeout_seconds=timeout),
            }
        gate = cls(
            level=cfg.security_level,
            channels=channels,
            confirm_channels=cfg.security_confirm_channels,
        )
        gate._fingerprint = cls._settings_fingerprint(cfg)
        return gate

    @staticmethod
    def _settings_fingerprint(cfg) -> tuple:
        return (
            cfg.security_level,
            tuple(cfg.security_confirm_channels),
            cfg.security_confirmation_timeout_sec,
            bool(cfg.telegram_bot_token),
            cfg.telegram_chat_id,
            cfg.telegram_api_base_url,
        )

    @classmethod
    def get_instance(cls) -> SecurityGate | None:
        return cls._instance

    @classmethod
    def get_or_create(cls, cfg) -> SecurityGate:
        """Return the gate that matches the live settings.

        A gate installed directly carries no fingerprint and is never
        replaced, so an embedding process keeps the channels it injected.
        """
        with cls._instance_lock:
            instance = cls._instance
            if instance is not None and (
                instance._fingerprint is None
                or instance._fingerprint == cls._settings_fingerprint(cfg)
            ):
                return instance
            return cls.from_settings(cfg)

    @classmethod
    def reset_instance(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def confirm(self, action_name: str, action_args: dict[str, Any]) -> bool:
        """Return whether policy and, when needed, the user allow an action."""
        if self.level == LEVEL_OFF:
            return True

        if not self._requires_confirmation(action_name, action_args):
            return True

        return self._request_confirmation(action_name, action_args)

    def _requires_confirmation(self, action_name: str, action_args: dict[str, Any]) -> bool:
        if self.level == LEVEL_PARANOID:
            return True
        if "__" in action_name:
            return True
        if action_name in _CRITICAL_BUILTINS:
            return True
        if action_name == "localFiles":
            # The tool strips and lowercases the operation before it acts, so
            # the gate has to read it exactly the same way.
            operation = action_args.get("operation")
            return isinstance(operation, str) and operation.strip().casefold() in _LOCAL_FILE_MUTATIONS
        return False

    def _request_confirmation(self, action_name: str, action_args: dict[str, Any]) -> bool:
        for channel_name in self._confirm_channels:
            channel = self._channels.get(channel_name)
            if channel is None:
                debug_log(f"security channel unavailable: {channel_name}", "security")
                continue
            try:
                available = channel.is_available
            except Exception as exc:
                debug_log(f"security channel availability failed: {channel_name}: {exc}", "security")
                continue
            if not available:
                debug_log(f"security channel unavailable: {channel_name}", "security")
                continue

            debug_log(f"security confirmation requested via {channel_name}: {action_name}", "security")
            try:
                approved = bool(channel.ask(action_name, action_args))
            except Exception as exc:
                debug_log(f"security channel failed before a decision: {channel_name}: {exc}", "security")
                continue

            decision = "approved" if approved else "denied"
            debug_log(f"security confirmation {decision}: {action_name}", "security")
            return approved

        debug_log(f"security confirmation denied with no available channel: {action_name}", "security")
        return False
