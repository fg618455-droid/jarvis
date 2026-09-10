"""Claude subscription adapter driving the CLI stream-json protocol."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol, Sequence

from jarvis.debug import debug_log

from .auth import AuthenticationManager, scrub_provider_environment
from .base import (
    AuthStatus,
    Capabilities,
    HealthReport,
    ModelInfo,
    ModelSource,
    NotSupported,
    ProviderAdapter,
    RunEvent,
    RunHandle,
    RunSpec,
    SessionInfo,
    UsageSnapshot,
)
from .models import ModelCatalog


PROBE_TIMEOUT_SECONDS = 120.0
SESSION_COMMAND_TIMEOUT_SECONDS = 20.0
STREAM_MESSAGE_TIMEOUT_SECONDS = 300.0
_PERMISSION_MODES: Mapping[str, str] = {
    "read_only": "plan",
    "project_dev": "acceptEdits",
    "automation": "dontAsk",
    "unrestricted": "bypassPermissions",
}


class ClaudeProcessError(RuntimeError):
    """Report Claude CLI output outside the shape the adapter understands."""


class ClaudeTimeoutError(ClaudeProcessError):
    """Report that Claude did not answer within the allotted time."""


def permission_mode_for(profile: str) -> str:
    """Translate a neutral capability profile into a native permission mode."""

    try:
        return _PERMISSION_MODES[profile]
    except KeyError as error:
        raise ValueError(f"Unsupported capability profile: {profile}") from error


class _Session(Protocol):
    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        """Return the next stream-json message."""

    def send_user_message(self, text: str) -> bool:
        """Send a further user message to the running session."""

    def interrupt(self) -> bool:
        """Stop the session."""

    def close(self) -> None:
        """Release the session process."""


class _Authentication(Protocol):
    def status(self, provider: str, *, force_refresh: bool = False) -> AuthStatus:
        """Return the fail-closed provider authentication state."""


@dataclass
class _RunState:
    handle: RunHandle
    session: _Session
    requested_model: str | None
    pending: list[RunEvent] = field(default_factory=list)
    terminal: bool = False


@dataclass(frozen=True)
class _RateLimit:
    """The most recent rate-limit reading Claude reported during a run."""

    status: str
    utilisation: float | None
    resets_at: str | None
    limit_type: str | None
    over_limit: bool


def _resolve_claude_command() -> tuple[str, ...]:
    """Resolve the platform-native launcher without a fixed installation path."""

    for name in ("claude.cmd", "claude") if os.name == "nt" else ("claude",):
        resolved = shutil.which(name)
        if resolved is not None:
            return (resolved,)
    raise FileNotFoundError("claude is not available on PATH")


class _CliSession:
    """One `claude -p` process streaming JSON for a single session."""

    def __init__(
        self,
        command: Sequence[str],
        spec: RunSpec,
        environment: Mapping[str, str],
        *,
        session_id: str,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        arguments = [
            *command,
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--session-id",
            session_id,
            "--permission-mode",
            permission_mode_for(spec.capability_profile),
        ]
        if spec.model is not None:
            arguments.extend(["--model", spec.model])
        if spec.system_prompt is not None:
            arguments.extend(["--append-system-prompt", spec.system_prompt])
        if spec.mcp_config is not None:
            arguments.extend(["--mcp-config", spec.mcp_config, "--strict-mcp-config"])
        for directory in spec.additional_directories:
            arguments.extend(["--add-dir", directory])
        if spec.allowed_tools:
            arguments.extend(["--allowedTools", *spec.allowed_tools])

        self._process = process_factory(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            cwd=spec.cwd,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._messages: queue.Queue[Any] = queue.Queue()
        self._write_lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._discard_stderr, daemon=True).start()
        self.send_user_message(spec.prompt)

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise ClaudeTimeoutError("Claude stopped sending events") from error
        if isinstance(message, Exception):
            raise message
        return message

    def send_user_message(self, text: str) -> bool:
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        with self._write_lock:
            stdin = self._process.stdin
            if stdin is None or self._process.poll() is not None:
                return False
            try:
                stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                stdin.flush()
            except (BrokenPipeError, OSError):
                return False
        return True

    def interrupt(self) -> bool:
        if self._process.poll() is not None:
            return False
        self._process.terminate()
        return True

    def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._messages.put(ClaudeProcessError("Claude stdout is unavailable"))
            return
        try:
            for line in stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._messages.put(ClaudeProcessError("Claude emitted malformed JSON"))
                    return
                if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                    self._messages.put(ClaudeProcessError("Claude emitted an unknown message"))
                    return
                self._messages.put(message)
        except (OSError, UnicodeError):
            pass
        self._messages.put(ClaudeProcessError("Claude stream ended before a result event"))

    def _discard_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for _line in stderr:
                pass
        except (OSError, UnicodeError):
            return


def _timestamp_from_unix(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def rate_limit_from_message(message: Mapping[str, Any]) -> _RateLimit | None:
    """Read the rate-limit reading Claude attaches to a running session."""

    info = message.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    status = info.get("status")
    if not isinstance(status, str):
        return None
    utilisation = info.get("utilization")
    return _RateLimit(
        status=status,
        utilisation=float(utilisation)
        if isinstance(utilisation, (int, float)) and not isinstance(utilisation, bool)
        else None,
        resets_at=_timestamp_from_unix(info.get("resetsAt")),
        limit_type=info.get("rateLimitType") if isinstance(info.get("rateLimitType"), str) else None,
        over_limit=not status.startswith("allowed"),
    )


def _usage_event(result: Mapping[str, Any]) -> RunEvent | None:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = []
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        counts.append(value)
    cached = usage.get("cache_read_input_tokens")
    created = usage.get("cache_creation_input_tokens")
    cached_total = sum(
        value for value in (cached, created) if isinstance(value, int) and not isinstance(value, bool)
    )
    return RunEvent(
        "usage.delta", {"input": counts[0], "output": counts[1], "cached": cached_total}
    )


def _result_status(result: Mapping[str, Any]) -> tuple[str, str]:
    if result.get("is_error") is not True and result.get("subtype") == "success":
        return "ok", "Claude finished the turn"
    if result.get("subtype") == "error_max_turns":
        return "error", "Claude reached its turn limit"
    api_error = result.get("api_error_status")
    if isinstance(api_error, int) and api_error == 429:
        return "quota", "Claude reported a rate limit"
    return "error", "Claude finished with an error"


def normalise_claude_message(message: Mapping[str, Any]) -> list[RunEvent]:
    """Normalise one Claude stream-json message into contract events."""

    message_type = message.get("type")
    if message_type == "stream_event":
        return _normalise_stream_event(message.get("event"))
    if message_type == "system":
        subtype = message.get("subtype")
        if subtype in {"permission_request", "permission_denial"}:
            tool_name = message.get("tool_name")
            return [
                RunEvent(
                    "approval.needed",
                    {
                        "kind": tool_name if isinstance(tool_name, str) else "tool",
                        "detail": "Claude requests approval before continuing",
                        "options": ["approve", "decline"],
                    },
                )
            ]
        return []
    if message_type == "user":
        return _normalise_tool_results(message)
    if message_type == "result":
        events: list[RunEvent] = []
        usage = _usage_event(message)
        if usage is not None:
            events.append(usage)
        status, reason = _result_status(message)
        events.append(RunEvent("run.finished", {"status": status, "reason": reason}))
        return events
    return []


def _normalise_stream_event(event: Any) -> list[RunEvent]:
    if not isinstance(event, dict):
        return []
    event_type = event.get("type")
    if event_type == "message_start":
        inner = event.get("message")
        identifier = inner.get("id") if isinstance(inner, dict) else None
        return [RunEvent("turn.started", {"turn_id": identifier if isinstance(identifier, str) else "turn"})]
    if event_type == "content_block_start":
        block = event.get("content_block")
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return []
        name, identifier = block.get("name"), block.get("id")
        return [
            RunEvent(
                "tool.call",
                {
                    "name": name if isinstance(name, str) else "tool",
                    "args_redacted": True,
                    "tool_id": identifier if isinstance(identifier, str) else "tool_use",
                },
            )
        ]
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return []
        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            return [RunEvent("text.delta", {"text": delta["text"]})]
        if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
            return [RunEvent("thinking", {"text": delta["thinking"]})]
    return []


def _normalise_tool_results(message: Mapping[str, Any]) -> list[RunEvent]:
    inner = message.get("message")
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return []
    events: list[RunEvent] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        identifier = block.get("tool_use_id")
        failed = block.get("is_error") is True
        payload = block.get("content")
        events.append(
            RunEvent(
                "tool.result",
                {
                    "tool_id": identifier if isinstance(identifier, str) else "tool_use",
                    "ok": not failed,
                    "summary": "Claude tool failed" if failed else "Claude tool completed",
                    "bytes": len(json.dumps(payload).encode("utf-8")) if payload is not None else 0,
                },
            )
        )
    return events


class ClaudeAdapter(ProviderAdapter):
    """Provider adapter for a claude.ai-authenticated Claude Code CLI."""

    def __init__(
        self,
        *,
        auth_manager: _Authentication | None = None,
        command: Sequence[str] | None = None,
        session_factory: Callable[[Sequence[str], RunSpec, Mapping[str, str]], _Session] | None = None,
        runner: Callable[..., Any] = subprocess.run,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._auth_manager = auth_manager or AuthenticationManager()
        self._session_factory = session_factory
        self._runner = runner
        self._process_factory = process_factory
        self._clock = clock
        self._runs: dict[str, _RunState] = {}
        self._owned_sessions: set[str] = set()
        self._models: dict[str, ModelSource] = {}
        self._capabilities = Capabilities()
        self._rate_limit: _RateLimit | None = None
        self._token_usage: tuple[int, int, int] | None = None

    def id(self) -> Literal["claude"]:
        return "claude"

    def auth_status(self) -> AuthStatus:
        return self._auth_manager.status("claude")

    def list_models(self) -> ModelCatalog:
        """Return only models a run has confirmed; Claude cannot enumerate."""

        return ModelCatalog(
            enumerable=False,
            models=tuple(
                ModelInfo(identifier, source) for identifier, source in sorted(self._models.items())
            ),
        )

    def verify_model(self, model: str) -> ModelInfo | None:
        """Confirm a model with a one-token probe, or record nothing at all."""

        self._require_subscription()
        command = [
            *(self._command or _resolve_claude_command()),
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--permission-mode",
            "plan",
            "ping",
        ]
        try:
            result = self._runner(
                command,
                capture_output=True,
                check=False,
                env=scrub_provider_environment(),
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            debug_log(f"Claude model probe failed to run: {type(error).__name__}", "providers")
            return None

        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            debug_log("Claude model probe returned output that is not JSON", "providers")
            return None
        if not isinstance(payload, dict) or payload.get("is_error") is not False:
            debug_log(f"Claude rejected the probe for model {model}", "providers")
            return None

        confirmed = self._canonical_model(payload)
        if confirmed is None:
            debug_log("Claude probe succeeded without naming a model", "providers")
            return None

        self._models[confirmed] = "verified-probe"
        debug_log(f"Verified Claude model {confirmed} by probe", "providers")
        return ModelInfo(confirmed, "verified-probe")

    @staticmethod
    def _canonical_model(payload: Mapping[str, Any]) -> str | None:
        """Read the model Claude actually billed, not the alias asked for."""

        usage = payload.get("modelUsage")
        if not isinstance(usage, dict) or not usage:
            return None
        name, detail = next(iter(usage.items()))
        if isinstance(detail, dict) and isinstance(detail.get("canonicalModel"), str):
            return detail["canonicalModel"]
        return name if isinstance(name, str) and name else None

    def capabilities(self, model: str | None) -> Capabilities:
        """Return the capabilities the last run reported, closed until then."""

        return self._capabilities

    def start_run(self, spec: RunSpec) -> RunHandle:
        if spec.skills or spec.toolsets:
            raise ClaudeProcessError(
                "Claude Code does not accept the skills or toolsets run fields"
            )
        self._require_subscription()
        session_id = str(uuid.uuid4())
        session = self._open_session(spec, session_id=session_id)
        handle = RunHandle(
            str(uuid.uuid4()),
            "claude",
            session_id,
            spec.model,
            spec.cwd,
            "verified-probe" if spec.model is not None else "provider-default",
        )
        self._owned_sessions.add(session_id)
        self._runs[handle.run_id] = _RunState(handle, session, spec.model)
        debug_log("Started Claude run through the stream-json CLI", "providers")
        return handle

    def stream(self, run: RunHandle) -> Iterator[RunEvent]:
        state = self._runs.get(run.run_id)
        if state is None or state.handle != run:
            raise ValueError("Unknown Claude run handle")
        while not state.terminal:
            try:
                message = state.session.next_message(STREAM_MESSAGE_TIMEOUT_SECONDS)
            except ClaudeTimeoutError:
                debug_log("Claude stream reached its message timeout", "providers")
                yield self._terminate(state, "timeout", "Claude stopped sending events")
                return
            except ClaudeProcessError:
                yield self._terminate(state, "error", "Claude stream ended without a result")
                return
            for event in self._events_for(state, message):
                if event.kind == "run.finished":
                    state.terminal = True
                yield event

    def steer(self, run: RunHandle, text: str) -> bool | NotSupported:
        state = self._runs.get(run.run_id)
        if state is None:
            return False
        return state.session.send_user_message(text)

    def interrupt(self, run: RunHandle) -> bool | NotSupported:
        state = self._runs.get(run.run_id)
        if state is None:
            return False
        return state.session.interrupt()

    def resume(self, session_id: str) -> RunHandle | NotSupported:
        """Reattach to a session; Claude needs a prompt to continue it."""

        return NotSupported(
            "resume", "Claude resumes a session only together with a new user message"
        )

    def fork(self, session_id: str) -> RunHandle | NotSupported:
        """Fork a session; Claude needs a prompt to continue it."""

        return NotSupported(
            "fork", "Claude forks a session only together with a new user message"
        )

    def list_sessions(self) -> list[SessionInfo] | NotSupported:
        command = [*(self._command or _resolve_claude_command()), "agents", "--json"]
        try:
            result = self._runner(
                command,
                capture_output=True,
                check=False,
                env=scrub_provider_environment(),
                text=True,
                timeout=SESSION_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ClaudeProcessError("Claude could not list its sessions") from error
        if result.returncode != 0:
            raise ClaudeProcessError("Claude exited with an error while listing sessions")
        try:
            listing = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise ClaudeProcessError("Claude session listing is not valid JSON") from error
        if not isinstance(listing, list):
            raise ClaudeProcessError("Claude session listing has an unexpected shape")
        return [self._session_info(entry) for entry in listing if isinstance(entry, dict)]

    def usage(self) -> UsageSnapshot:
        """Return the last reading a run produced; Claude has no query command."""

        if self._rate_limit is None and self._token_usage is None:
            return UsageSnapshot(
                detail="Claude reports usage only during a run, and none has run yet"
            )
        tokens = self._token_usage or (None, None, None)
        if self._rate_limit is None:
            return UsageSnapshot(
                available=True,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                cached_tokens=tokens[2],
                detail="Claude token counts from the last run",
            )
        utilisation = self._rate_limit.utilisation
        return UsageSnapshot(
            available=True,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cached_tokens=tokens[2],
            limit=100.0 if utilisation is not None else None,
            remaining=max(0.0, 100.0 - utilisation * 100.0) if utilisation is not None else None,
            resets_at=self._rate_limit.resets_at,
            detail=f"Claude {self._rate_limit.limit_type or 'rate'} window, status {self._rate_limit.status}",
        )

    def health(self) -> HealthReport:
        checked_at = datetime.now(timezone.utc).isoformat()
        started = self._clock()
        status = self._auth_manager.status("claude", force_refresh=True)
        latency_ms = (self._clock() - started) * 1000.0
        if not status.logged_in:
            return HealthReport(
                reachable=False,
                latency_ms=latency_ms,
                detail="Claude is not signed in to a claude.ai subscription",
                checked_at=checked_at,
            )
        return HealthReport(
            reachable=True,
            latency_ms=latency_ms,
            detail="Claude CLI reports a signed-in subscription",
            checked_at=checked_at,
        )

    def close(self) -> None:
        for state in self._runs.values():
            state.session.close()

    def _open_session(
        self, spec: RunSpec, *, session_id: str
    ) -> _Session:
        environment = scrub_provider_environment()
        command = self._command or _resolve_claude_command()
        if self._session_factory is not None:
            return self._session_factory(command, spec, environment)
        return _CliSession(
            command,
            spec,
            environment,
            session_id=session_id,
            process_factory=self._process_factory,
        )

    def _require_subscription(self) -> AuthStatus:
        status = self._auth_manager.status("claude")
        if not status.logged_in or status.method != "claude.ai":
            debug_log("Claude run blocked because claude.ai subscription auth is absent", "providers")
            raise PermissionError("Claude requires claude.ai subscription authentication")
        return status

    def _events_for(self, state: _RunState, message: Mapping[str, Any]) -> list[RunEvent]:
        if message.get("type") == "system" and message.get("subtype") == "init":
            return [self._run_started(state, message)]
        if message.get("type") == "rate_limit_event":
            reading = rate_limit_from_message(message)
            if reading is None:
                return []
            self._rate_limit = reading
            if reading.over_limit:
                debug_log("Claude reported an exhausted rate-limit window", "providers")
                return [self._terminate(state, "quota", "Claude rate limit is exhausted")]
            return []
        if message.get("type") == "result":
            usage = message.get("usage")
            if isinstance(usage, dict):
                self._record_tokens(usage)
        return normalise_claude_message(message)

    def _run_started(self, state: _RunState, message: Mapping[str, Any]) -> RunEvent:
        model = message.get("model")
        if isinstance(model, str) and model:
            source: ModelSource = (
                "verified-probe" if state.requested_model == model else "provider-default"
            )
            if self._models.get(model) != "verified-probe":
                self._models[model] = source
            state.handle = RunHandle(
                state.handle.run_id,
                "claude",
                state.handle.session_id,
                model,
                state.handle.cwd,
                state.handle.model_source,
            )
        self._record_capabilities(message)
        return RunEvent(
            "run.started",
            {
                "run_id": state.handle.run_id,
                "provider": "claude",
                "model": state.handle.model,
                "session_id": state.handle.session_id,
                "cwd": state.handle.cwd,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _record_capabilities(self, message: Mapping[str, Any]) -> None:
        tools = message.get("tools")
        servers = message.get("mcp_servers")
        self._capabilities = Capabilities(
            tools=isinstance(tools, list) and bool(tools),
            mcp=isinstance(servers, list) and bool(servers),
            streaming=True,
            images=False,
            structured_output=True,
            steering=True,
        )

    def _record_tokens(self, usage: Mapping[str, Any]) -> None:
        def count(name: str) -> int:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        self._token_usage = (
            count("input_tokens"),
            count("output_tokens"),
            count("cache_read_input_tokens") + count("cache_creation_input_tokens"),
        )

    def _terminate(self, state: _RunState, status: str, reason: str) -> RunEvent:
        state.terminal = True
        return RunEvent("run.finished", {"status": status, "reason": reason})

    def _session_info(self, entry: Mapping[str, Any]) -> SessionInfo:
        session_id = entry.get("sessionId")
        identifier = session_id if isinstance(session_id, str) else str(entry.get("id", ""))
        name, state = entry.get("name"), entry.get("state")
        return SessionInfo(
            session_id=identifier,
            provider="claude",
            owned=identifier in self._owned_sessions,
            title=name if isinstance(name, str) else None,
            model=None,
            cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
            status=state if isinstance(state, str) else None,
            started_at=_timestamp_from_unix(
                entry["startedAt"] / 1000 if isinstance(entry.get("startedAt"), int) else None
            ),
        )
