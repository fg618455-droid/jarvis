"""Fail-closed subscription authentication through provider CLIs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from jarvis.debug import debug_log

from .base import AuthStatus, ProviderId


AUTH_CACHE_TTL_SECONDS = 60.0
AUTH_COMMAND_TIMEOUT_SECONDS = 10.0
_BILLING_KEY_NAMES = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})


def scrub_provider_environment(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy an environment without API billing credentials."""

    source = os.environ if parent is None else parent
    return {key: value for key, value in source.items() if key.upper() not in _BILLING_KEY_NAMES}


def parse_claude_auth_status(output: str) -> AuthStatus:
    """Parse the documented JSON output from ``claude auth status``."""

    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        debug_log("Claude auth status was not valid JSON", "providers")
        return AuthStatus()

    if not isinstance(payload, dict) or not isinstance(payload.get("loggedIn"), bool):
        debug_log("Claude auth status did not match the expected schema", "providers")
        return AuthStatus()

    if payload["loggedIn"] is False:
        return AuthStatus()

    method = payload.get("authMethod")
    if method != "claude.ai":
        debug_log("Claude auth status did not report claude.ai", "providers")
        return AuthStatus(method=method if isinstance(method, str) else None)

    email = payload.get("email")
    organisation = payload.get("orgId")
    plan = payload.get("subscriptionType")
    return AuthStatus(
        logged_in=True,
        method="claude.ai",
        account=email if isinstance(email, str) else organisation if isinstance(organisation, str) else None,
        plan=plan if isinstance(plan, str) else None,
    )


def parse_codex_auth_status(output: str) -> AuthStatus:
    """Parse ``codex login status`` without accepting ambiguous text."""

    status_line = output.strip()
    if status_line == "Logged in using ChatGPT":
        return AuthStatus(logged_in=True, method="chatgpt")
    if status_line == "Logged in using API key":
        debug_log("Codex auth status reported API-key authentication", "providers")
        return AuthStatus(method="api_key")
    if status_line != "Not logged in":
        debug_log("Codex auth status did not match the expected format", "providers")
    return AuthStatus()


def parse_hermes_auth_status(output: str, provider: str) -> AuthStatus:
    """Parse the Hermes text status for its supported subscription route."""

    if output.strip() == f"{provider}: logged in":
        return AuthStatus(logged_in=True, method="chatgpt", plan=provider)
    return AuthStatus()


def _default_hermes_command() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "hermes" / "bin" / "hermes.cmd"
        if candidate.is_file():
            return str(candidate)
    return "hermes"


def _default_hermes_config_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / "hermes" / "config.yaml"


def _read_hermes_provider(config_path: Path | None) -> str | None:
    """Read the scalar ``model.provider`` value from Hermes YAML config."""

    if config_path is None:
        return None
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        debug_log("Hermes provider configuration is unavailable", "providers")
        return None

    model_indent: int | None = None
    provider_values: list[str] = []
    value_pattern = re.compile(r'''^(?:["']?)([A-Za-z0-9][A-Za-z0-9._-]*)(?:["']?)(?:\s+#.*)?$''')
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if model_indent is None:
            if re.fullmatch(r"model\s*:\s*(?:#.*)?", stripped):
                model_indent = indent
            continue
        if indent <= model_indent:
            break
        match = re.fullmatch(r"provider\s*:\s*(.*)", stripped)
        if match is None:
            continue
        value_match = value_pattern.fullmatch(match.group(1).strip())
        if value_match is not None:
            provider_values.append(value_match.group(1))

    if len(provider_values) != 1:
        return None
    return provider_values[0]


class AuthenticationManager:
    """Read and cache provider-owned subscription authentication state."""

    def __init__(
        self,
        *,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl_seconds: float = AUTH_CACHE_TTL_SECONDS,
        hermes_command: str | None = None,
        hermes_config_path: str | Path | None = None,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._cache_ttl_seconds = cache_ttl_seconds
        self._hermes_command = hermes_command or _default_hermes_command()
        self._hermes_config_path = (
            Path(hermes_config_path) if hermes_config_path is not None else _default_hermes_config_path()
        )
        self._cache: dict[ProviderId, tuple[float, AuthStatus]] = {}
        self._lock = RLock()

    def status(self, provider: ProviderId, *, force_refresh: bool = False) -> AuthStatus:
        """Return cached authentication state or query the provider CLI."""

        if provider not in {"claude", "codex", "hermes"}:
            raise ValueError(f"Unsupported provider: {provider}")

        with self._lock:
            now = self._clock()
            cached = self._cache.get(provider)
            if not force_refresh and cached is not None and now - cached[0] < self._cache_ttl_seconds:
                debug_log(f"Using cached {provider} authentication state", "providers")
                return cached[1]

            debug_log(f"Refreshing {provider} authentication state", "providers")
            status = self._query(provider)
            self._cache[provider] = (now, status)
            return status

    def invalidate(self, provider: ProviderId | None = None) -> None:
        """Invalidate one provider cache entry or the complete cache."""

        with self._lock:
            if provider is None:
                self._cache.clear()
                return
            if provider not in {"claude", "codex", "hermes"}:
                raise ValueError(f"Unsupported provider: {provider}")
            self._cache.pop(provider, None)

    def _query(self, provider: ProviderId) -> AuthStatus:
        commands: dict[ProviderId, list[str]] = {
            "claude": ["claude", "auth", "status"],
            "codex": ["codex", "login", "status"],
            "hermes": [self._hermes_command, "auth", "status"],
        }
        parsers: dict[ProviderId, Callable[[str], AuthStatus]] = {
            "claude": parse_claude_auth_status,
            "codex": parse_codex_auth_status,
            "hermes": lambda output: AuthStatus(),
        }
        if provider == "hermes":
            hermes_provider = _read_hermes_provider(self._hermes_config_path)
            if hermes_provider is None:
                debug_log("Hermes authentication cannot run without a configured provider", "providers")
                return AuthStatus(detail="hermes provider unknown")
            commands["hermes"].append(hermes_provider)
            parsers["hermes"] = lambda output: parse_hermes_auth_status(output, hermes_provider)

        try:
            result = self._runner(
                commands[provider],
                capture_output=True,
                check=False,
                env=scrub_provider_environment(),
                text=True,
                timeout=AUTH_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            debug_log(
                f"{provider} authentication command failed to start: {type(error).__name__}",
                "providers",
            )
            return AuthStatus()

        if result.returncode != 0:
            debug_log(
                f"{provider} authentication command exited with status {result.returncode}",
                "providers",
            )
            return AuthStatus()
        return parsers[provider](result.stdout)
