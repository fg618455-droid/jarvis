"""Subscription-backed Codex CLI implementation of :class:`LLMBackend`.

The backend authenticates through the existing Codex CLI login and never
reads or supplies an API key.  It is a CHAT-tier text generator: Jarvis owns
the native tool loop, so a supplied tool schema is rejected before a child
process starts.

Each request runs in a fresh empty temporary directory with the installed
CLI's user configuration disabled.  The command line explicitly selects the
read-only sandbox, a non-interactive approval policy, an ephemeral session,
and the configured model.  The prompt travels over stdin rather than in the
argument vector.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .backend import (
    AuthError,
    LLMBackend,
    ModelUnavailableError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    ToolsNotSupportedError,
)
from .claude_subscription import _flatten_messages


_REASONING_EFFORT = "low"
_PROCESS_STOP_GRACE_SEC = 1.0
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_DIRECT_AUTH_ENVIRONMENT = frozenset({
    "OPENAI_ACCESS_TOKEN",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "CODEX_BASE_URL",
})


def _combine_prompt(system_prompt: str, prompt: str) -> str:
    parts = [part.strip() for part in (system_prompt, prompt) if part and part.strip()]
    return "\n\n".join(parts)


def _map_cli_failure(diagnostic: str) -> ProviderError:
    """Classify private CLI diagnostics without returning their contents."""
    text = diagnostic.casefold()
    if any(marker in text for marker in (
        "insufficient_quota", "quota exhausted", "quota exceeded", "usage quota",
    )):
        return QuotaExhaustedError()
    if any(marker in text for marker in (
        "rate limit", "too many requests", "status 429", "http 429", "error 429",
    )):
        return RateLimitedError()
    if any(marker in text for marker in (
        "unauthorized", "unauthenticated", "authentication", "not logged in",
        "login expired", "session expired", "status 401", "status 403",
        "http 401", "http 403",
    )):
        return AuthError("provider rejected the active session")
    if "model" in text and any(marker in text for marker in (
        "not found", "unknown", "unavailable", "does not exist", "unsupported",
    )):
        return ModelUnavailableError("configured model is unavailable")
    return ProviderError("provider request failed")


def _resolve_codex_launcher() -> List[str]:
    """Return a directly executable Codex launcher on this platform."""
    codex = shutil.which("codex")
    if os.name != "nt" or not codex or Path(codex).suffix.casefold() == ".exe":
        return [codex or "codex"]

    # npm's Windows entry point is a .cmd shim, which CreateProcess cannot
    # execute directly with shell=False. Launch its JavaScript entry point
    # through Node so no command shell or interpolated command string exists.
    node = shutil.which("node")
    if node:
        search_roots = [Path(codex).parent]
        search_roots.extend(
            Path(item)
            for item in os.environ.get("PATH", "").split(os.pathsep)
            if item
        )
        for root in dict.fromkeys(search_roots):
            entry_point = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            if entry_point.is_file():
                return [node, str(entry_point)]
    return [codex]


def _subscription_environment() -> Dict[str, str]:
    """Inherit the CLI login location but exclude direct API credentials."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().endswith("_API_KEY")
        and key.upper() not in _DIRECT_AUTH_ENVIRONMENT
    }


def _terminate_and_reap(process: subprocess.Popen[str]) -> None:
    """Stop the Codex process group and wait until the child is reaped."""
    if process.poll() is not None:
        process.wait()
        return

    try:
        if os.name == "nt" and hasattr(process, "send_signal"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        elif hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=_PROCESS_STOP_GRACE_SEC)
        return
    except Exception:
        pass

    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait()
    except Exception:
        pass


class CodexSubscriptionBackend(LLMBackend):
    """Text-generation backend using an existing Codex subscription login."""

    @staticmethod
    def _command(model: str, working_directory: str) -> List[str]:
        if not _MODEL_NAME.fullmatch(model or ""):
            raise ProviderError("configured model is unavailable")
        return _resolve_codex_launcher() + [
            "exec",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            working_directory,
            "--model",
            model,
            "-c",
            'approval_policy="never"',
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'web_search="disabled"',
            "-c",
            "features.shell_tool=false",
            "-c",
            f'model_reasoning_effort="{_REASONING_EFFORT}"',
            "--json",
            "-",
        ]

    @staticmethod
    def _log_selected(method: str) -> None:
        debug_log(f"CodexSubscriptionBackend: selected for {method}", "llm")

    def _run(
        self,
        model: str,
        prompt: str,
        timeout_sec: float,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        timeout = max(0.01, float(timeout_sec))
        deadline_at = time.monotonic() + timeout
        text_parts: List[str] = []
        stdout_diagnostics: List[str] = []
        stderr_diagnostics: List[str] = []
        reader_failures: List[str] = []

        with tempfile.TemporaryDirectory(prefix="jarvis-codex-") as working_directory:
            command = self._command(model, working_directory)
            popen_options: Dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "cwd": working_directory,
                "env": _subscription_environment(),
            }
            if os.name == "nt":
                popen_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            else:
                popen_options["start_new_session"] = True

            try:
                process = subprocess.Popen(command, **popen_options)
            except Exception as error:
                debug_log(
                    "CodexSubscriptionBackend: process start failed "
                    f"({type(error).__name__})",
                    "llm",
                )
                raise ProviderError("provider is not available") from None

            debug_log(
                "CodexSubscriptionBackend: process started in isolated read-only mode",
                "llm",
            )

            def read_stdout() -> None:
                try:
                    for line in process.stdout or ():
                        stdout_diagnostics.append(line)
                        try:
                            event = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(event, dict) or event.get("type") != "item.completed":
                            continue
                        item = event.get("item")
                        if not isinstance(item, dict) or item.get("type") != "agent_message":
                            continue
                        text = item.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        text_parts.append(text)
                        if on_token is not None:
                            try:
                                on_token(text)
                            except Exception as listener_error:
                                debug_log(
                                    "CodexSubscriptionBackend: token listener failed "
                                    f"({type(listener_error).__name__})",
                                    "llm",
                                )
                except Exception as error:
                    reader_failures.append(type(error).__name__)

            def read_stderr() -> None:
                try:
                    for line in process.stderr or ():
                        stderr_diagnostics.append(line)
                except Exception as error:
                    reader_failures.append(type(error).__name__)

            stdout_thread = threading.Thread(
                target=read_stdout, daemon=True, name="jarvis-codex-stdout"
            )
            stderr_thread = threading.Thread(
                target=read_stderr, daemon=True, name="jarvis-codex-stderr"
            )
            stdout_thread.start()
            stderr_thread.start()

            try:
                if process.stdin is None:
                    raise OSError("stdin pipe unavailable")
                process.stdin.write(prompt)
                process.stdin.close()
                remaining = max(0.0, deadline_at - time.monotonic())
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _terminate_and_reap(process)
                stdout_thread.join(timeout=_PROCESS_STOP_GRACE_SEC)
                stderr_thread.join(timeout=_PROCESS_STOP_GRACE_SEC)
                debug_log(
                    "CodexSubscriptionBackend: request timed out and process was reaped",
                    "llm",
                )
                raise ProviderError("provider request timed out") from None
            except Exception as error:
                _terminate_and_reap(process)
                stdout_thread.join(timeout=_PROCESS_STOP_GRACE_SEC)
                stderr_thread.join(timeout=_PROCESS_STOP_GRACE_SEC)
                debug_log(
                    "CodexSubscriptionBackend: process communication failed "
                    f"({type(error).__name__})",
                    "llm",
                )
                raise ProviderError("provider request failed") from None

            stdout_thread.join(timeout=_PROCESS_STOP_GRACE_SEC)
            stderr_thread.join(timeout=_PROCESS_STOP_GRACE_SEC)

            if reader_failures:
                debug_log(
                    "CodexSubscriptionBackend: process output could not be read",
                    "llm",
                )
                raise ProviderError("provider request failed")

            if process.returncode != 0:
                error = _map_cli_failure(
                    "".join(stdout_diagnostics) + "\n" + "".join(stderr_diagnostics)
                )
                debug_log(
                    "CodexSubscriptionBackend: provider returned "
                    f"{type(error).__name__}",
                    "llm",
                )
                raise error

        text = "".join(text_parts)
        return text if text.strip() else None

    def direct(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        timeout_sec: float = 10.0,
        thinking: bool = False,
        num_ctx: int = 4096,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        self._log_selected("direct")
        return self._run(
            chat_model,
            _combine_prompt(system_prompt, user_content),
            timeout_sec,
        )

    def streaming(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        on_token: Optional[Callable[[str], None]] = None,
        timeout_sec: float = 30.0,
        thinking: bool = False,
    ) -> Optional[str]:
        self._log_selected("streaming")
        return self._run(
            chat_model,
            _combine_prompt(system_prompt, user_content),
            timeout_sec,
            on_token,
        )

    def chat(
        self,
        chat_model: str,
        messages: List[Dict[str, Any]],
        timeout_sec: float = 30.0,
        extra_options: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        if tools is not None:
            debug_log(
                "CodexSubscriptionBackend: denied a native tool schema "
                "(text-generation-only)",
                "llm",
            )
            raise ToolsNotSupportedError(
                "codex_subscription is text-generation only and never accepts native tools"
            )
        self._log_selected("chat")
        system_prompt, transcript = _flatten_messages(messages)
        text = self._run(
            chat_model,
            _combine_prompt(system_prompt, transcript),
            timeout_sec,
            on_token,
        )
        if text is None:
            return None
        message = {"role": "assistant", "content": text}
        return {"choices": [{"message": message}], "message": message}

    def embed(
        self, text: str, model: str, timeout_sec: float = 15.0
    ) -> Optional[List[float]]:
        return None

    def list_models(self, timeout_sec: float = 5.0) -> List[str]:
        return []

    def warm_up(
        self,
        model: str,
        timeout_sec: float = 60.0,
        keep_alive: str = "30m",
    ) -> bool:
        return True
