"""Main-process client for the isolated Claude subscription sidecar.

The Claude Agent SDK and its dependency set never enter this interpreter.
The client starts a dedicated Python environment lazily and exchanges one
newline-delimited JSON request at a time over the child process's pipes.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..debug import debug_log


_DEFAULT_ENV_DIR = "claude-subscription-venv"
_INTERPRETER_ENV = "JARVIS_CLAUDE_SIDECAR_PYTHON"
_EOF = object()
_SAFE_TOOL_NAME = re.compile(r"[^A-Za-z0-9_.:-]")


class ClaudeSidecarError(Exception):
    """A sanitised sidecar transport or provider failure."""

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def default_sidecar_interpreter() -> Path:
    """Resolve the configured or conventional sidecar interpreter path."""
    configured = os.environ.get(_INTERPRETER_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    root = Path.home() / ".jarvis" / _DEFAULT_ENV_DIR
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


class ClaudeSubscriptionSidecarClient:
    """Own one lazy, serial Claude sidecar subprocess."""

    def __init__(
        self,
        *,
        interpreter_path: Optional[Path] = None,
        entrypoint_path: Optional[Path] = None,
        ready_timeout_sec: float = 20.0,
    ) -> None:
        self._interpreter_path = interpreter_path or default_sidecar_interpreter()
        self._entrypoint_path = entrypoint_path or Path(__file__).with_name(
            "claude_subscription_sidecar.py"
        )
        self._ready_timeout_sec = max(0.01, float(ready_timeout_sec))
        self._process: Optional[subprocess.Popen[str]] = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0
        atexit.register(self.stop)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @staticmethod
    def _read_stdout(
        process: subprocess.Popen[str],
        messages: queue.Queue[object],
    ) -> None:
        stream = process.stdout
        if stream is not None:
            try:
                for line in iter(stream.readline, ""):
                    messages.put(line)
            except Exception:
                pass
        messages.put(_EOF)

    def _read_message(self, timeout_sec: float) -> dict:
        try:
            item = self._messages.get(timeout=max(0.01, timeout_sec))
        except queue.Empty:
            raise ClaudeSidecarError("Claude sidecar response timed out") from None
        if item is _EOF:
            raise ClaudeSidecarError("Claude sidecar stopped unexpectedly")
        try:
            message = json.loads(str(item))
        except (TypeError, ValueError):
            raise ClaudeSidecarError("Claude sidecar sent an invalid response") from None
        if not isinstance(message, dict):
            raise ClaudeSidecarError("Claude sidecar sent an invalid response")
        return message

    def _drop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
        except Exception:
            pass
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def _ensure_started(self, deadline: float) -> None:
        if self.is_running:
            return
        self._drop_process()
        self._messages = queue.Queue()
        if not self._interpreter_path.is_file():
            debug_log("Claude subscription sidecar environment is unavailable", "llm")
            raise ClaudeSidecarError(
                "Claude subscription sidecar environment is not installed"
            )
        if not self._entrypoint_path.is_file():
            debug_log("Claude subscription sidecar entry point is unavailable", "llm")
            raise ClaudeSidecarError("Claude subscription sidecar is not available")

        environment = os.environ.copy()
        environment.pop("ANTHROPIC_API_KEY", None)
        environment["PYTHONIOENCODING"] = "utf-8"
        debug_log("ClaudeSubscriptionBackend: launching isolated sidecar", "llm")
        try:
            process = subprocess.Popen(
                [
                    str(self._interpreter_path),
                    "-I",
                    "-X",
                    "utf8",
                    "-u",
                    str(self._entrypoint_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as error:
            debug_log(
                "Claude subscription sidecar launch failed "
                f"({type(error).__name__})",
                "llm",
            )
            raise ClaudeSidecarError("Claude subscription sidecar is not available") from None

        self._process = process
        threading.Thread(
            target=self._read_stdout,
            args=(process, self._messages),
            name="ClaudeSubscriptionSidecarOutput",
            daemon=True,
        ).start()
        try:
            ready = self._read_message(
                min(
                    self._ready_timeout_sec,
                    max(0.01, deadline - time.monotonic()),
                )
            )
        except ClaudeSidecarError:
            self._drop_process()
            raise ClaudeSidecarError(
                "Claude subscription sidecar did not become ready"
            ) from None
        if ready.get("type") != "ready":
            self._drop_process()
            raise ClaudeSidecarError(
                "Claude subscription sidecar did not become ready"
            )
        debug_log("ClaudeSubscriptionBackend: isolated sidecar ready", "llm")

    def _write(self, payload: dict) -> None:
        process = self._process
        try:
            if process is None or process.stdin is None:
                raise BrokenPipeError
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except Exception:
            self._drop_process()
            raise ClaudeSidecarError("Claude sidecar pipe failed") from None

    def generate(
        self,
        model: str,
        system_prompt: str,
        prompt: str,
        timeout_sec: float,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """Run one generation and return its assembled text."""
        with self._lock:
            deadline = time.monotonic() + max(0.1, float(timeout_sec))
            self._ensure_started(deadline)
            self._next_id += 1
            request_id = self._next_id
            self._write({
                "cmd": "generate",
                "id": request_id,
                "model": model,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "stream": on_token is not None,
            })
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._drop_process()
                    raise ClaudeSidecarError("Claude sidecar response timed out")
                try:
                    message = self._read_message(remaining)
                except ClaudeSidecarError:
                    self._drop_process()
                    raise
                if message.get("id") != request_id:
                    self._drop_process()
                    raise ClaudeSidecarError("Claude sidecar protocol failed")

                kind = message.get("type")
                if kind == "chunk":
                    chunk = message.get("text")
                    if isinstance(chunk, str) and chunk and on_token is not None:
                        try:
                            on_token(chunk)
                        except Exception as error:
                            debug_log(
                                "ClaudeSubscriptionBackend: token listener failed "
                                f"({type(error).__name__})",
                                "llm",
                            )
                    continue
                if kind == "tool_denied":
                    raw_name = message.get("tool_name", "unknown")
                    safe_name = _SAFE_TOOL_NAME.sub("?", str(raw_name))[:100]
                    debug_log(
                        "ClaudeSubscriptionBackend: denied a tool-use attempt "
                        f"inside the sidecar ({safe_name})",
                        "llm",
                    )
                    continue
                if kind == "error":
                    status = message.get("status")
                    raise ClaudeSidecarError(
                        "Claude subscription provider request failed",
                        status=status if isinstance(status, int) else None,
                    )
                if kind == "result":
                    text = message.get("text")
                    if not isinstance(text, str):
                        self._drop_process()
                        raise ClaudeSidecarError("Claude sidecar protocol failed")
                    return text if text.strip() else None
                self._drop_process()
                raise ClaudeSidecarError("Claude sidecar protocol failed")

    def stop(self) -> None:
        """Ask the child to exit and clean up its pipes."""
        with self._lock:
            process = self._process
            if process is None:
                return
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write('{"cmd":"shutdown"}\n')
                        process.stdin.flush()
                    process.wait(timeout=3.0)
                except Exception:
                    pass
            self._drop_process()
