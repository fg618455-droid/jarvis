"""Hermes subscription adapter using Agent Client Protocol over stdio."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol, Sequence

from jarvis.debug import debug_log

from .auth import AuthenticationManager, _default_hermes_command, scrub_provider_environment
from .base import (
    AuthStatus,
    Capabilities,
    HealthReport,
    ModelInfo,
    NotSupported,
    ProviderAdapter,
    RunEvent,
    RunHandle,
    RunSpec,
    SessionInfo,
    UsageSnapshot,
)
from .models import ModelCatalog


ACP_REQUEST_TIMEOUT_SECONDS = 60.0
ACP_STREAM_TIMEOUT_SECONDS = 300.0


class HermesProtocolError(RuntimeError):
    """Report an ACP or catalogue response outside the known schema."""


class HermesTimeoutError(HermesProtocolError):
    """Report that Hermes did not answer within the allotted time."""


@dataclass(frozen=True)
class HermesTransportState:
    """Visible state of the only safe Hermes transport."""

    mode: Literal["acp", "unavailable"]
    detail: str


class _Transport(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send one request and return its result."""

    def begin_request(self, method: str, params: dict[str, Any]) -> int:
        """Send a request whose response belongs to the event stream."""

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        """Return the next ACP notification, request or deferred response."""

    def respond(self, request_id: int, result: dict[str, Any]) -> None:
        """Answer an ACP request from Hermes."""

    def close(self) -> None:
        """Close the transport."""


class _Authentication(Protocol):
    def status(self, provider: str, *, force_refresh: bool = False) -> AuthStatus:
        """Return the fail-closed provider authentication state."""


@dataclass
class _RunState:
    handle: RunHandle
    prompt_request_id: int | None
    initial_events: list[RunEvent]
    terminal: bool = False


class _AcpTransport:
    """One long-lived Hermes ACP JSON-RPC v2 connection."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        request_timeout: float,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self._request_timeout = request_timeout
        self._request_id = 0
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._streaming_ids: set[int] = set()
        self._messages: queue.Queue[Any] = queue.Queue()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._process = process_factory(
            [*command, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if self._process.stdin is None or self._process.stdout is None:
            self.close()
            raise HermesProtocolError("Hermes ACP stdio is unavailable")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._discard_stderr, daemon=True).start()

    def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id, response_queue = self._prepare_request()
        self._write_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            response = response_queue.get(timeout=self._request_timeout)
        except queue.Empty as error:
            raise HermesTimeoutError(f"Hermes ACP timed out during {method}") from error
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        return self._result(response, method)

    def begin_request(self, method: str, params: dict[str, Any]) -> int:
        if self._closed:
            raise HermesProtocolError("Hermes ACP transport is closed")
        with self._state_lock:
            self._request_id += 1
            request_id = self._request_id
            self._streaming_ids.add(request_id)
        self._write_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return request_id

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise HermesTimeoutError("Hermes ACP stream timed out") from error
        if isinstance(message, Exception):
            raise message
        if not isinstance(message, dict):
            raise HermesProtocolError("Hermes ACP emitted an invalid message")
        return message

    def respond(self, request_id: int, result: dict[str, Any]) -> None:
        self._write_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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

    def _prepare_request(self) -> tuple[int, queue.Queue[Any]]:
        if self._closed:
            raise HermesProtocolError("Hermes ACP transport is closed")
        with self._state_lock:
            self._request_id += 1
            request_id = self._request_id
            response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        return request_id, response_queue

    def _write_message(self, message: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
            with self._write_lock:
                stdin = self._process.stdin
                if stdin is None:
                    raise HermesProtocolError("Hermes ACP stdin is unavailable")
                stdin.write(encoded)
                stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise HermesProtocolError("Hermes ACP could not write to stdio") from error

    @staticmethod
    def _result(response: Any, method: str) -> Any:
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, dict):
            raise HermesProtocolError(f"Hermes ACP returned an invalid response for {method}")
        if "error" in response:
            error_value = response["error"]
            code = error_value.get("code") if isinstance(error_value, dict) else None
            raise HermesProtocolError(f"Hermes ACP rejected {method} with code {code}")
        if "result" not in response:
            raise HermesProtocolError(f"Hermes ACP omitted the result for {method}")
        return response["result"]

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._fail_all(HermesProtocolError("Hermes ACP stdout is unavailable"))
            return
        try:
            for line in stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._fail_all(HermesProtocolError("Hermes ACP emitted malformed JSON"))
                    return
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    self._fail_all(HermesProtocolError("Hermes ACP emitted invalid JSON-RPC"))
                    return
                request_id = message.get("id")
                if request_id is not None and "method" not in message:
                    with self._state_lock:
                        response_queue = self._pending.get(request_id)
                        is_streaming = request_id in self._streaming_ids
                        if is_streaming:
                            self._streaming_ids.discard(request_id)
                    if response_queue is not None:
                        response_queue.put(message)
                    elif is_streaming:
                        self._messages.put(message)
                    continue
                if isinstance(message.get("method"), str):
                    self._messages.put(message)
                    continue
                self._fail_all(HermesProtocolError("Hermes ACP emitted an unknown message"))
                return
        except (OSError, UnicodeError):
            pass
        self._fail_all(HermesProtocolError("Hermes ACP stream closed"))

    def _discard_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for _line in stderr:
                pass
        except (OSError, UnicodeError):
            return

    def _fail_all(self, error: HermesProtocolError) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
        for response_queue in pending:
            response_queue.put(error)
        self._messages.put(error)


def _as_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise HermesProtocolError(f"Hermes {context} response has an unexpected shape")
    return value


def _required_string(payload: Mapping[str, Any], field: str, context: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise HermesProtocolError(f"Hermes {context} response has an invalid {field}")
    return value


def _profile_mode(profile: str) -> str:
    modes = {
        "read_only": "default",
        "project_dev": "accept_edits",
        "automation": "dont_ask",
    }
    if profile == "unrestricted":
        raise HermesProtocolError(
            "Hermes ACP cannot represent the unrestricted capability profile without --yolo"
        )
    try:
        return modes[profile]
    except KeyError as error:
        raise ValueError(f"Unsupported capability profile: {profile}") from error


def _usage_event(usage: Any) -> RunEvent | None:
    if not isinstance(usage, dict):
        return None
    values: list[int] = []
    for field in ("inputTokens", "outputTokens", "cachedReadTokens"):
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values.append(value)
    return RunEvent(
        "usage.delta", {"input": values[0], "output": values[1], "cached": values[2]}
    )


def _prompt_terminal(result: Mapping[str, Any]) -> RunEvent:
    stop_reason = result.get("stopReason")
    if stop_reason == "end_turn":
        status, reason = "ok", "Hermes finished the turn"
    elif stop_reason == "cancelled":
        status, reason = "cancelled", "Hermes cancelled the turn"
    else:
        status, reason = "error", "Hermes finished with an unrecognised stop reason"
    return RunEvent("run.finished", {"status": status, "reason": reason})


def normalise_hermes_message(
    message: Mapping[str, Any], *, prompt_request_id: int | None
) -> list[RunEvent]:
    """Normalise one observed Hermes ACP wire message into contract events."""

    if message.get("id") == prompt_request_id and "method" not in message:
        if "error" in message:
            return [
                RunEvent(
                    "run.finished",
                    {"status": "error", "reason": "Hermes rejected the prompt request"},
                )
            ]
        result = message.get("result")
        if not isinstance(result, dict):
            return [
                RunEvent(
                    "run.finished",
                    {"status": "error", "reason": "Hermes returned an invalid prompt result"},
                )
            ]
        events: list[RunEvent] = []
        usage = _usage_event(result.get("usage"))
        if usage is not None:
            events.append(usage)
        events.append(_prompt_terminal(result))
        return events

    method = message.get("method")
    params = message.get("params")
    if method == "session/request_permission" and isinstance(params, dict):
        tool_call = params.get("toolCall")
        tool = tool_call if isinstance(tool_call, dict) else {}
        options_value = params.get("options")
        options = []
        if isinstance(options_value, list):
            for option in options_value:
                if isinstance(option, dict) and isinstance(option.get("optionId"), str):
                    options.append(option["optionId"])
        kind = tool.get("kind") if isinstance(tool.get("kind"), str) else "tool"
        title = tool.get("title") if isinstance(tool.get("title"), str) else "Hermes requests approval"
        return [
            RunEvent(
                "approval.needed",
                {"kind": kind, "detail": title, "options": options},
            )
        ]
    if method != "session/update" or not isinstance(params, dict):
        return []
    update_value = params.get("update")
    if not isinstance(update_value, dict):
        return []
    kind = update_value.get("sessionUpdate")
    content = update_value.get("content")
    if kind == "agent_message_chunk" and isinstance(content, dict):
        text = content.get("text")
        return [RunEvent("text.delta", {"text": text})] if isinstance(text, str) else []
    if kind == "agent_thought_chunk" and isinstance(content, dict):
        text = content.get("text")
        return [RunEvent("thinking", {"text": text})] if isinstance(text, str) else []
    if kind == "usage_update":
        return []
    if kind == "tool_call":
        identifier = update_value.get("toolCallId")
        title = update_value.get("title")
        if not isinstance(identifier, str):
            return []
        return [
            RunEvent(
                "tool.call",
                {
                    "name": title if isinstance(title, str) else "tool",
                    "args_redacted": True,
                    "tool_id": identifier,
                },
            )
        ]
    if kind == "tool_call_update":
        identifier = update_value.get("toolCallId")
        status = update_value.get("status")
        if not isinstance(identifier, str) or status not in {"completed", "failed"}:
            return []
        encoded = json.dumps(content, ensure_ascii=False).encode("utf-8") if content is not None else b""
        return [
            RunEvent(
                "tool.result",
                {
                    "tool_id": identifier,
                    "ok": status == "completed",
                    "summary": "Hermes tool completed" if status == "completed" else "Hermes tool failed",
                    "bytes": len(encoded),
                },
            )
        ]
    return []


class HermesAdapter(ProviderAdapter):
    """Provider adapter for Hermes through its ACP stdio server."""

    def __init__(
        self,
        *,
        auth_manager: _Authentication | None = None,
        command: Sequence[str] | None = None,
        transport: _Transport | None = None,
        transport_state: HermesTransportState | None = None,
        runner: Callable[..., Any] = subprocess.run,
        request_timeout: float = ACP_REQUEST_TIMEOUT_SECONDS,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._auth_manager = auth_manager or AuthenticationManager()
        self._transport = transport
        self._transport_state = transport_state or HermesTransportState(
            "unavailable", "Hermes ACP transport has not started"
        )
        self._runner = runner
        self._request_timeout = request_timeout
        self._process_factory = process_factory
        self._clock = clock
        self._initialised = False
        self._capabilities = Capabilities()
        self._session_capabilities: frozenset[str] = frozenset()
        self._agent_name: str | None = None
        self._agent_version: str | None = None
        self._runs: dict[str, _RunState] = {}
        self._owned_sessions: set[str] = set()
        self._models: tuple[ModelInfo, ...] = ()
        self._token_usage: tuple[int, int, int] | None = None
        self._context_usage: tuple[int, int] | None = None

    def id(self) -> Literal["hermes"]:
        return "hermes"

    def auth_status(self) -> AuthStatus:
        return self._auth_manager.status("hermes")

    def transport_state(self) -> HermesTransportState:
        return self._transport_state

    def list_models(self) -> ModelCatalog:
        """Return the models ACP advertises, opening a session if needed."""

        if not self._models:
            transport = self._acp()
            session = _as_mapping(
                transport.request("session/new", {"cwd": os.getcwd(), "mcpServers": []}),
                "session/new",
            )
            self._record_catalogue(_as_mapping(session.get("models"), "session/new models"))
        return ModelCatalog(enumerable=True, models=self._models)

    def capabilities(self, model: str | None) -> Capabilities:
        return self._capabilities

    def start_run(self, spec: RunSpec) -> RunHandle:
        unsupported = []
        if spec.system_prompt is not None:
            unsupported.append("system_prompt")
        if spec.mcp_config is not None:
            unsupported.append("mcp_config")
        for name, values in {
            "allowed_tools": spec.allowed_tools,
            "additional_directories": spec.additional_directories,
            "skills": spec.skills,
            "toolsets": spec.toolsets,
        }.items():
            if values:
                unsupported.append(name)
        if unsupported:
            raise HermesProtocolError(
                f"Hermes ACP does not wire RunSpec fields: {', '.join(unsupported)}"
            )
        self._require_subscription()
        transport = self._acp()
        cwd = spec.cwd or os.getcwd()
        session = _as_mapping(
            transport.request("session/new", {"cwd": cwd, "mcpServers": []}),
            "session/new",
        )
        session_id = _required_string(session, "sessionId", "session/new")
        models = _as_mapping(session.get("models"), "session/new models")
        available_ids = self._record_catalogue(models)
        current_model = models.get("currentModelId")
        if current_model is not None and not isinstance(current_model, str):
            raise HermesProtocolError("Hermes session/new returned an invalid currentModelId")
        actual_model = current_model
        model_source: Literal["api", "provider-default", "unknown"] = (
            "provider-default" if isinstance(current_model, str) and current_model else "unknown"
        )
        if spec.model is not None:
            if spec.model not in available_ids:
                raise ValueError("The requested Hermes model is not advertised by ACP")
            _as_mapping(
                transport.request(
                    "session/set_model", {"sessionId": session_id, "modelId": spec.model}
                ),
                "session/set_model",
            )
            actual_model = spec.model
            model_source = "api"
        mode = _profile_mode(spec.capability_profile)
        modes = _as_mapping(session.get("modes"), "session/new modes")
        available_modes = modes.get("availableModes")
        mode_ids = {
            value.get("id")
            for value in available_modes
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        } if isinstance(available_modes, list) else set()
        if mode not in mode_ids:
            raise HermesProtocolError(
                f"Hermes ACP does not advertise the mode required for {spec.capability_profile}"
            )
        _as_mapping(
            transport.request(
                "session/set_mode", {"sessionId": session_id, "modeId": mode}
            ),
            "session/set_mode",
        )
        run_id = str(uuid.uuid4())
        handle = RunHandle(
            run_id, "hermes", session_id, actual_model, cwd, model_source
        )
        prompt_request_id = transport.begin_request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": spec.prompt}],
            },
        )
        self._owned_sessions.add(session_id)
        self._runs[run_id] = _RunState(
            handle,
            prompt_request_id,
            [
                RunEvent(
                    "run.started",
                    {
                        "run_id": run_id,
                        "provider": "hermes",
                        "model": actual_model,
                        "session_id": session_id,
                        "cwd": cwd,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                ),
                RunEvent("turn.started", {"turn_id": run_id}),
            ],
        )
        debug_log("Started Hermes run through ACP", "providers")
        return handle

    def stream(self, run: RunHandle) -> Iterator[RunEvent]:
        state = self._runs.get(run.run_id)
        if state is None or state.handle != run:
            raise ValueError("Unknown Hermes run handle")
        while state.initial_events:
            yield state.initial_events.pop(0)
        while not state.terminal:
            try:
                message = self._acp().next_message(ACP_STREAM_TIMEOUT_SECONDS)
            except HermesTimeoutError:
                debug_log("Hermes ACP stream reached its message timeout", "providers")
                yield self._terminate(state, "timeout", "Hermes stopped sending events")
                return
            except HermesProtocolError:
                yield self._terminate(state, "error", "Hermes ACP stream transport failed")
                return
            if not self._message_matches_run(message, state):
                continue
            self._record_usage(message, state.prompt_request_id)
            events = normalise_hermes_message(
                message, prompt_request_id=state.prompt_request_id
            )
            if message.get("method") == "session/request_permission":
                request_id = message.get("id")
                if isinstance(request_id, int):
                    self._acp().respond(
                        request_id, {"outcome": {"outcome": "cancelled"}}
                    )
            for event in events:
                if event.kind == "run.finished":
                    state.terminal = True
                yield event

    def steer(self, run: RunHandle, text: str) -> bool | NotSupported:
        return NotSupported("steer", "Hermes ACP does not advertise mid-run steering")

    def interrupt(self, run: RunHandle) -> bool | NotSupported:
        if run.run_id not in self._runs:
            return False
        self._acp().request("session/cancel", {"sessionId": run.session_id})
        return True

    def resume(self, session_id: str) -> RunHandle | NotSupported:
        self._ensure_transport()
        if "resume" not in self._session_capabilities:
            return NotSupported("resume", "Hermes ACP does not advertise session resume")
        response = self._acp().request(
            "session/resume",
            {"sessionId": session_id, "cwd": os.getcwd(), "mcpServers": []},
        )
        mapping = _as_mapping(response, "session/resume")
        model = self._current_model(mapping)
        handle = RunHandle(
            str(uuid.uuid4()),
            "hermes",
            session_id,
            model,
            os.getcwd(),
            "api" if model is not None else "unknown",
        )
        self._owned_sessions.add(session_id)
        return handle

    def fork(self, session_id: str) -> RunHandle | NotSupported:
        self._ensure_transport()
        if "fork" not in self._session_capabilities:
            return NotSupported("fork", "Hermes ACP does not advertise session fork")
        response = _as_mapping(
            self._acp().request(
                "session/fork",
                {"sessionId": session_id, "cwd": os.getcwd(), "mcpServers": []},
            ),
            "session/fork",
        )
        new_session_id = _required_string(response, "sessionId", "session/fork")
        model = self._current_model(response)
        handle = RunHandle(
            str(uuid.uuid4()),
            "hermes",
            new_session_id,
            model,
            os.getcwd(),
            "api" if model is not None else "unknown",
        )
        self._owned_sessions.add(new_session_id)
        return handle

    def list_sessions(self) -> list[SessionInfo] | NotSupported:
        self._ensure_transport()
        if "list" not in self._session_capabilities:
            return NotSupported("list_sessions", "Hermes ACP does not advertise session listing")
        sessions: list[SessionInfo] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            response = _as_mapping(
                self._acp().request("session/list", params), "session/list"
            )
            entries = response.get("sessions")
            if not isinstance(entries, list):
                raise HermesProtocolError("Hermes session/list returned invalid sessions")
            for entry_value in entries:
                entry = _as_mapping(entry_value, "session/list entry")
                identifier = _required_string(entry, "sessionId", "session/list")
                sessions.append(
                    SessionInfo(
                        session_id=identifier,
                        provider="hermes",
                        owned=identifier in self._owned_sessions,
                        title=entry.get("title") if isinstance(entry.get("title"), str) else None,
                        cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
                        started_at=entry.get("updatedAt") if isinstance(entry.get("updatedAt"), str) else None,
                    )
                )
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise HermesProtocolError("Hermes session/list returned an invalid nextCursor")
            cursor = next_cursor
        return sessions

    def usage(self) -> UsageSnapshot:
        if self._token_usage is None and self._context_usage is None:
            return UsageSnapshot(
                detail="Hermes reports usage only during a run, and none has run yet"
            )
        tokens = self._token_usage or (None, None, None)
        size, used = self._context_usage or (None, None)
        context = f"; context {used} of {size}" if size is not None and used is not None else ""
        return UsageSnapshot(
            available=True,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cached_tokens=tokens[2],
            detail=(
                f"Hermes token counts from the last run{context}. Hermes exposes no quota "
                "window of its own: it spends the subscription of the provider configured in "
                "its own settings, which it shares with whichever adapter uses that provider."
            ),
        )

    def health(self) -> HealthReport:
        checked_at = datetime.now(timezone.utc).isoformat()
        started = self._clock()
        try:
            self._ensure_transport()
        except (HermesProtocolError, OSError, subprocess.SubprocessError) as error:
            debug_log(f"Hermes health check failed: {type(error).__name__}", "providers")
            return HealthReport(
                False,
                detail=self._transport_state.detail,
                checked_at=checked_at,
            )
        identity = " ".join(value for value in (self._agent_name, self._agent_version) if value)
        detail = "Hermes ACP transport is active"
        if identity:
            detail = f"{detail}: {identity}"
        return HealthReport(
            True,
            (self._clock() - started) * 1000.0,
            detail,
            checked_at,
        )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def _require_subscription(self) -> AuthStatus:
        status = self._auth_manager.status("hermes")
        if not status.logged_in or status.method != "chatgpt":
            debug_log("Hermes run blocked because subscription auth is absent", "providers")
            raise PermissionError("Hermes requires configured subscription authentication")
        return status

    def _acp(self) -> _Transport:
        self._ensure_transport()
        if self._transport is None or self._transport_state.mode != "acp":
            raise HermesProtocolError(self._transport_state.detail)
        return self._transport

    def _ensure_transport(self) -> None:
        if self._initialised and self._transport is not None:
            return
        if self._transport is None:
            self._require_subscription()
            command = self._command or (_default_hermes_command(),)
            debug_log("Starting Hermes ACP transport", "providers")
            try:
                self._transport = _AcpTransport(
                    command,
                    environment=scrub_provider_environment(),
                    request_timeout=self._request_timeout,
                    process_factory=self._process_factory,
                )
            except (OSError, HermesProtocolError) as error:
                self._transport_state = HermesTransportState(
                    "unavailable", "Hermes ACP transport could not start"
                )
                raise HermesProtocolError(self._transport_state.detail) from error
        try:
            result = _as_mapping(
                self._transport.request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False}
                        },
                        "clientInfo": {
                            "name": "jarvis",
                            "title": "JARVIS",
                            "version": "1",
                        },
                    },
                ),
                "initialize",
            )
            self._record_handshake(result)
        except (HermesProtocolError, HermesTimeoutError) as error:
            self._transport_state = HermesTransportState(
                "unavailable", "Hermes ACP initialisation failed"
            )
            self._transport.close()
            raise HermesProtocolError(self._transport_state.detail) from error
        self._initialised = True
        self._transport_state = HermesTransportState(
            "acp", "Hermes ACP JSON-RPC transport is active"
        )

    def _record_handshake(self, result: Mapping[str, Any]) -> None:
        capabilities = _as_mapping(result.get("agentCapabilities"), "initialize capabilities")
        prompt_capabilities = capabilities.get("promptCapabilities")
        prompt = prompt_capabilities if isinstance(prompt_capabilities, dict) else {}
        session_capabilities = capabilities.get("sessionCapabilities")
        session = session_capabilities if isinstance(session_capabilities, dict) else {}
        self._session_capabilities = frozenset(
            name for name in ("fork", "list", "resume") if isinstance(session.get(name), dict)
        )
        self._capabilities = Capabilities(
            streaming=True,
            images=prompt.get("image") is True,
            steering=False,
        )
        agent_info = result.get("agentInfo")
        if isinstance(agent_info, dict):
            name, version = agent_info.get("name"), agent_info.get("version")
            self._agent_name = name if isinstance(name, str) else None
            self._agent_version = version if isinstance(version, str) else None

    def _record_catalogue(self, models: Mapping[str, Any]) -> set[str]:
        """Store the advertised catalogue and return its identifiers."""

        entries = models.get("availableModels")
        if not isinstance(entries, list):
            raise HermesProtocolError("Hermes session/new returned invalid availableModels")
        catalogue: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise HermesProtocolError("Hermes session/new returned an invalid model")
            identifier = _required_string(entry, "modelId", "session/new model")
            name = entry.get("name")
            if name is not None and not isinstance(name, str):
                raise HermesProtocolError("Hermes session/new returned an invalid model name")
            catalogue.append(ModelInfo(identifier, "api", name))
        self._models = tuple(catalogue)
        return {model.id for model in catalogue}

    @staticmethod
    def _current_model(response: Mapping[str, Any]) -> str | None:
        models = response.get("models")
        if not isinstance(models, dict):
            return None
        current = models.get("currentModelId")
        return current if isinstance(current, str) and current else None

    def _record_usage(
        self, message: Mapping[str, Any], prompt_request_id: int | None
    ) -> None:
        if message.get("method") == "session/update":
            params = message.get("params")
            update = params.get("update") if isinstance(params, dict) else None
            if isinstance(update, dict) and update.get("sessionUpdate") == "usage_update":
                size, used = update.get("size"), update.get("used")
                if all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in (size, used)
                ):
                    self._context_usage = (size, used)
        if message.get("id") == prompt_request_id:
            result = message.get("result")
            usage = result.get("usage") if isinstance(result, dict) else None
            if isinstance(usage, dict):
                values = tuple(usage.get(name) for name in ("inputTokens", "outputTokens", "cachedReadTokens"))
                if all(
                    isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    for value in values
                ):
                    self._token_usage = values  # type: ignore[assignment]

    @staticmethod
    def _message_matches_run(message: Mapping[str, Any], state: _RunState) -> bool:
        if message.get("id") == state.prompt_request_id and "method" not in message:
            return True
        params = message.get("params")
        if not isinstance(params, dict):
            return False
        return params.get("sessionId") == state.handle.session_id

    @staticmethod
    def _terminate(state: _RunState, status: str, reason: str) -> RunEvent:
        state.terminal = True
        return RunEvent("run.finished", {"status": status, "reason": reason})
