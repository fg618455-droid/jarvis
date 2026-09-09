"""Codex subscription adapter using the generated app-server v2 protocol."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol, Sequence, cast

from jarvis.debug import debug_log

from .auth import AuthenticationManager, scrub_provider_environment
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


APP_SERVER_REQUEST_TIMEOUT_SECONDS = 10.0
STREAM_MESSAGE_TIMEOUT_SECONDS = 300.0
_QUOTA_ERROR_CODES = frozenset(
    {"sessionBudgetExceeded", "usageLimitExceeded", "rateLimitExceeded"}
)
_TOOL_ITEM_TYPES = frozenset(
    {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "webSearch",
        "imageView",
        "imageGeneration",
    }
)


class CodexProtocolError(RuntimeError):
    """Report an app-server or JSONL response outside the known schema."""


class CodexTimeoutError(CodexProtocolError):
    """Report that Codex did not answer within the allotted time."""


@dataclass(frozen=True)
class CodexTransportState:
    """Visible transport selection for the adapter."""

    mode: Literal["app-server", "exec-jsonl", "unavailable"]
    detail: str


class _Transport(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send one request and return its result."""

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        """Return the next server notification or request."""

    def close(self) -> None:
        """Close the transport."""


class _Authentication(Protocol):
    def status(self, provider: str, *, force_refresh: bool = False) -> AuthStatus:
        """Return the fail-closed provider authentication state."""


@dataclass
class _RunState:
    handle: RunHandle
    turn_id: str | None
    initial_events: list[RunEvent]
    process: _ExecJsonProcess | None = None
    terminal: bool = False


def _resolve_codex_command() -> tuple[str, ...]:
    """Resolve the platform-native launcher without a fixed installation path."""

    name = "codex.cmd" if os.name == "nt" else "codex"
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(f"{name} is not available on PATH")
    return (resolved,)


def _codex_auth_runner(
    command: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run authentication with the adapter's platform-aware resolution."""

    resolved = list(command)
    if resolved and resolved[0] == "codex":
        resolved[0] = _resolve_codex_command()[0]
    result = subprocess.run(resolved, **kwargs)
    if result.stdout.strip():
        return result
    stderr_lines = result.stderr.splitlines()
    if stderr_lines and stderr_lines[-1] in {
        "Logged in using ChatGPT",
        "Logged in using API key",
        "Not logged in",
    }:
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            stdout=f"{stderr_lines[-1]}\n",
            stderr="\n".join(stderr_lines[:-1]),
        )
    return result


class _JsonRpcTransport:
    """One long-lived JSON-RPC v2 connection to ``codex app-server``."""

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
        self._messages: queue.Queue[Any] = queue.Queue()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._process = process_factory(
            [*command, "app-server"],
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
            raise CodexProtocolError("Codex app-server stdio is unavailable")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._discard_stderr, daemon=True).start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "jarvis", "title": "JARVIS", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
        except Exception:
            self.close()
            raise

    def request(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed:
            raise CodexProtocolError("Codex app-server transport is closed")
        with self._state_lock:
            self._request_id += 1
            request_id = self._request_id
            response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False) + "\n"
            with self._write_lock:
                stdin = self._process.stdin
                if stdin is None:
                    raise CodexProtocolError("Codex app-server stdin is unavailable")
                stdin.write(encoded)
                stdin.flush()
            response = response_queue.get(timeout=self._request_timeout)
        except queue.Empty as error:
            raise CodexTimeoutError(f"Codex app-server timed out during {method}") from error
        except (BrokenPipeError, OSError) as error:
            raise CodexProtocolError(f"Codex app-server could not send {method}") from error
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, dict):
            raise CodexProtocolError(f"Codex app-server returned an invalid response for {method}")
        if "error" in response:
            error_payload = response["error"]
            code = error_payload.get("code") if isinstance(error_payload, dict) else None
            raise CodexProtocolError(f"Codex app-server rejected {method} with code {code}")
        if "result" not in response:
            raise CodexProtocolError(f"Codex app-server omitted the result for {method}")
        return response["result"]

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise CodexTimeoutError("Codex app-server stream timed out") from error
        if isinstance(message, Exception):
            raise message
        if not isinstance(message, dict):
            raise CodexProtocolError("Codex app-server emitted an invalid message")
        return message

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

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._fail_all(CodexProtocolError("Codex app-server stdout is unavailable"))
            return
        try:
            for line in stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._fail_all(CodexProtocolError("Codex app-server emitted malformed JSON"))
                    return
                if not isinstance(message, dict) or message.get("jsonrpc") not in (None, "2.0"):
                    self._fail_all(CodexProtocolError("Codex app-server emitted invalid JSON-RPC"))
                    return
                request_id = message.get("id")
                if request_id is not None and "method" not in message:
                    with self._state_lock:
                        response_queue = self._pending.get(request_id)
                    if response_queue is not None:
                        response_queue.put(message)
                    continue
                if isinstance(message.get("method"), str):
                    self._messages.put(message)
                    continue
                self._fail_all(CodexProtocolError("Codex app-server emitted an unknown message"))
                return
        except (OSError, UnicodeError):
            pass
        self._fail_all(CodexProtocolError("Codex app-server stream closed"))

    def _discard_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for _line in stderr:
                pass
        except (OSError, UnicodeError):
            return

    def _fail_all(self, error: CodexProtocolError) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
        for response_queue in pending:
            response_queue.put(error)
        self._messages.put(error)


def _permission_settings(profile: str) -> tuple[str, str]:
    settings = {
        "read_only": ("read-only", "on-request"),
        "project_dev": ("workspace-write", "on-request"),
        "automation": ("workspace-write", "never"),
        "unrestricted": ("danger-full-access", "on-request"),
    }
    try:
        return settings[profile]
    except KeyError as error:
        raise ValueError(f"Unsupported capability profile: {profile}") from error


class _ExecJsonProcess:
    """One ``codex exec --json`` fallback run."""

    def __init__(
        self,
        command: Sequence[str],
        spec: RunSpec,
        *,
        environment: Mapping[str, str],
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        sandbox, _approval_policy = _permission_settings(spec.capability_profile)
        arguments = [*command, "exec", "--json", "--color", "never", "--sandbox", sandbox]
        if spec.model is not None:
            arguments.extend(["--model", spec.model])
        if spec.cwd is not None:
            arguments.extend(["--cd", spec.cwd])
        arguments.append(spec.prompt)
        self._process = process_factory(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._messages: queue.Queue[Any] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._discard_stderr, daemon=True).start()

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise CodexTimeoutError("Codex exec JSONL stream timed out") from error
        if isinstance(message, Exception):
            raise message
        return cast(dict[str, Any], message)

    def interrupt(self) -> bool:
        if self._process.poll() is not None:
            return False
        self._process.terminate()
        return True

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._messages.put(CodexProtocolError("Codex exec stdout is unavailable"))
            return
        try:
            for line in stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._messages.put(CodexProtocolError("Codex exec emitted malformed JSONL"))
                    return
                if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                    self._messages.put(CodexProtocolError("Codex exec emitted invalid JSONL"))
                    return
                self._messages.put(message)
        finally:
            self._messages.put(
                CodexProtocolError(f"Codex exec stream closed with status {self._process.wait()}")
            )

    def _discard_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for _line in stderr:
                pass
        except (OSError, UnicodeError):
            return


def _as_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CodexProtocolError(f"Codex {context} response has an invalid shape")
    return value


def _required_string(payload: Mapping[str, Any], field: str, context: object) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise CodexProtocolError(f"Codex {context} omitted {field}")
    return value


def _timestamp_from_unix(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _tool_name(item: Mapping[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "commandExecution":
        return "command"
    if item_type == "fileChange":
        return "apply_patch"
    if item_type == "mcpToolCall":
        server, tool = item.get("server"), item.get("tool")
        return f"{server}/{tool}" if isinstance(server, str) and isinstance(tool, str) else "mcp"
    if item_type == "dynamicToolCall":
        namespace, tool = item.get("namespace"), item.get("tool")
        if isinstance(namespace, str) and isinstance(tool, str):
            return f"{namespace}/{tool}"
        return tool if isinstance(tool, str) else "dynamic_tool"
    if item_type == "collabAgentToolCall":
        tool = item.get("tool")
        return tool if isinstance(tool, str) else "collaboration"
    return {
        "webSearch": "web_search",
        "imageView": "view_image",
        "imageGeneration": "image_generation",
    }.get(cast(str, item_type), "tool")


def _tool_result(item: Mapping[str, Any]) -> RunEvent:
    item_type = item.get("type")
    tool_id = _required_string(item, "id", "item/completed")
    success, size = True, 0
    if item_type == "commandExecution":
        success = item.get("status") == "completed" and item.get("exitCode") in (0, None)
        output = item.get("aggregatedOutput")
        size = len(output.encode("utf-8")) if isinstance(output, str) else 0
    elif item_type == "fileChange":
        success = item.get("status") == "completed"
    elif item_type == "mcpToolCall":
        success = item.get("status") == "completed" and item.get("error") is None
    elif item_type == "dynamicToolCall":
        success = item.get("status") == "completed" and item.get("success") is not False
    elif item_type == "imageGeneration":
        success = item.get("failure") is None and item.get("status") == "completed"
    return RunEvent(
        "tool.result",
        {
            "tool_id": tool_id,
            "ok": success,
            "summary": "Codex tool completed" if success else "Codex tool failed",
            "bytes": size,
        },
    )


def _turn_status(turn: Mapping[str, Any]) -> tuple[str, str]:
    status = turn.get("status")
    if status == "completed":
        return "ok", "Codex turn completed"
    if status == "interrupted":
        return "cancelled", "Codex turn was interrupted"
    if status != "failed":
        return "error", "Codex returned an unknown terminal state"
    error = turn.get("error")
    error_info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if error_info in _QUOTA_ERROR_CODES:
        return "quota", "Codex quota is exhausted"
    return "error", "Codex turn failed"


def normalise_codex_message(message: Mapping[str, Any]) -> list[RunEvent]:
    """Normalise one generated-schema app-server message into contract events."""

    method = message.get("method")
    params = _as_mapping(message.get("params"), str(method))
    if method == "turn/started":
        turn = _as_mapping(params.get("turn"), "turn/started")
        return [RunEvent("turn.started", {"turn_id": _required_string(turn, "id", method)})]
    if method == "item/agentMessage/delta":
        return [RunEvent("text.delta", {"text": _required_string(params, "delta", method)})]
    if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
        return [RunEvent("thinking", {"text": _required_string(params, "delta", method)})]
    if method == "thread/tokenUsage/updated":
        usage = _as_mapping(params.get("tokenUsage"), str(method))
        last = _as_mapping(usage.get("last"), str(method))
        values = [last.get(name) for name in ("inputTokens", "outputTokens", "cachedInputTokens")]
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise CodexProtocolError(f"Codex {method} omitted token counts")
        return [
            RunEvent(
                "usage.delta",
                {"input": values[0], "output": values[1], "cached": values[2]},
            )
        ]
    if method == "item/started":
        item = _as_mapping(params.get("item"), str(method))
        if item.get("type") not in _TOOL_ITEM_TYPES:
            return []
        return [
            RunEvent(
                "tool.call",
                {
                    "name": _tool_name(item),
                    "args_redacted": True,
                    "tool_id": _required_string(item, "id", method),
                },
            )
        ]
    if method == "item/completed":
        item = _as_mapping(params.get("item"), str(method))
        return [] if item.get("type") not in _TOOL_ITEM_TYPES else [_tool_result(item)]
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
    }:
        return [
            RunEvent(
                "approval.needed",
                {
                    "kind": str(method),
                    "detail": "Codex requests approval before continuing",
                    "options": ["approve", "decline"],
                },
            )
        ]
    if method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
        questions = params.get("questions")
        titles = (
            [
                question.get("header")
                or question.get("question")
                or question.get("title")
                for question in questions
                if isinstance(question, dict)
            ]
            if isinstance(questions, list)
            else []
        )
        question = next((title for title in titles if isinstance(title, str) and title), None)
        return [RunEvent("needs_you", {"question": question or "Codex requires user input"})]
    if method == "turn/completed":
        turn = _as_mapping(params.get("turn"), str(method))
        status, reason = _turn_status(turn)
        return [RunEvent("run.finished", {"status": status, "reason": reason})]
    if method == "error":
        if params.get("willRetry") is True:
            return []
        error = params.get("error")
        error_info = error.get("codexErrorInfo") if isinstance(error, dict) else None
        status = "quota" if error_info in _QUOTA_ERROR_CODES else "error"
        reason = "Codex quota is exhausted" if status == "quota" else "Codex turn failed"
        return [RunEvent("run.finished", {"status": status, "reason": reason})]
    return []


def _normalise_exec_message(message: Mapping[str, Any]) -> list[RunEvent]:
    event_type = message.get("type")
    if event_type == "turn.started":
        return [RunEvent("turn.started", {"turn_id": str(message.get("turn_id", "turn"))})]
    if event_type == "item.completed":
        item = _as_mapping(message.get("item"), "exec item.completed")
        if item.get("type") == "agent_message":
            return [RunEvent("text.delta", {"text": _required_string(item, "text", event_type)})]
    if event_type == "turn.completed":
        usage = message.get("usage")
        events: list[RunEvent] = []
        if isinstance(usage, dict):
            events.append(
                RunEvent(
                    "usage.delta",
                    {
                        "input": int(usage.get("input_tokens", 0)),
                        "output": int(usage.get("output_tokens", 0)),
                        "cached": int(usage.get("cached_input_tokens", 0)),
                    },
                )
            )
        events.append(RunEvent("run.finished", {"status": "ok", "reason": "Codex turn completed"}))
        return events
    if event_type == "turn.failed":
        error = message.get("error")
        code = error.get("codex_error_info") if isinstance(error, dict) else None
        status = "quota" if code in {"usage_limit_exceeded", "rate_limit_exceeded"} else "error"
        reason = "Codex quota is exhausted" if status == "quota" else "Codex turn failed"
        return [RunEvent("run.finished", {"status": status, "reason": reason})]
    return []


class CodexAdapter(ProviderAdapter):
    """Provider adapter for a ChatGPT-authenticated Codex CLI."""

    def __init__(
        self,
        *,
        auth_manager: _Authentication | None = None,
        command: Sequence[str] | None = None,
        transport: _Transport | None = None,
        transport_state: CodexTransportState | None = None,
        request_timeout: float = APP_SERVER_REQUEST_TIMEOUT_SECONDS,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._auth_manager = auth_manager or AuthenticationManager(runner=_codex_auth_runner)
        self._transport = transport
        self._transport_state = transport_state or (
            CodexTransportState("app-server", "Codex app-server JSON-RPC transport is active")
            if transport is not None
            else CodexTransportState("unavailable", "Codex transport has not started")
        )
        self._request_timeout = request_timeout
        self._process_factory = process_factory
        self._clock = clock
        self._runs: dict[str, _RunState] = {}
        self._owned_sessions: set[str] = set()
        self._deferred_messages: list[dict[str, Any]] = []
        self._server_auth_verified = False
        self._catalogue_ids: set[str] = set()

    def id(self) -> Literal["codex"]:
        return "codex"

    def auth_status(self) -> AuthStatus:
        return self._auth_manager.status("codex")

    def transport_state(self) -> CodexTransportState:
        """Return the selected transport and any visible degradation."""

        return self._transport_state

    def list_models(self) -> ModelCatalog:
        transport = self._app_server()
        models: list[ModelInfo] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            response = _as_mapping(transport.request("model/list", params), "model/list")
            data = response.get("data")
            if not isinstance(data, list):
                raise CodexProtocolError("Codex model/list response has an invalid data field")
            for entry_value in data:
                entry = _as_mapping(entry_value, "model/list")
                model_id = _required_string(entry, "id", "model/list")
                _required_string(entry, "model", "model/list")
                display_name = entry.get("displayName")
                if display_name is not None and not isinstance(display_name, str):
                    raise CodexProtocolError("Codex model/list returned an invalid displayName")
                models.append(ModelInfo(model_id, "api", display_name))
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise CodexProtocolError("Codex model/list returned an invalid nextCursor")
            cursor = next_cursor
        self._catalogue_ids = {model.id for model in models}
        return ModelCatalog(enumerable=True, models=tuple(models))

    def capabilities(self, model: str | None) -> Capabilities:
        response = self._app_server().request("modelProvider/capabilities/read", {})
        if not isinstance(response, dict):
            debug_log("Codex capability response has an unknown shape", "providers")
            return Capabilities()
        return Capabilities(
            tools=response.get("namespaceTools") is True or response.get("webSearch") is True,
            images=response.get("imageGeneration") is True,
        )

    def start_run(self, spec: RunSpec) -> RunHandle:
        populated = []
        if spec.mcp_config is not None:
            populated.append("mcp_config")
        for name, values in {
            "allowed_tools": spec.allowed_tools,
            "additional_directories": spec.additional_directories,
            "skills": spec.skills,
            "toolsets": spec.toolsets,
        }.items():
            if values:
                populated.append(name)
        if populated:
            raise CodexProtocolError(
                f"Codex app-server does not wire RunSpec fields: {', '.join(populated)}"
            )
        self._require_subscription()
        if self._transport_state.mode == "exec-jsonl":
            self._validate_fallback_model(spec.model)
            return self._start_exec_run(spec)
        try:
            transport = self._app_server()
        except CodexProtocolError:
            if self._transport_state.mode != "exec-jsonl":
                raise
            self._validate_fallback_model(spec.model)
            return self._start_exec_run(spec)
        if spec.model is not None and spec.model not in {
            model.id for model in self.list_models().models
        }:
            raise ValueError("The requested Codex model is not present in model/list")
        sandbox, approval_policy = _permission_settings(spec.capability_profile)
        thread_params: dict[str, Any] = {"sandbox": sandbox, "approvalPolicy": approval_policy}
        if spec.cwd is not None:
            thread_params["cwd"] = spec.cwd
        if spec.model is not None:
            thread_params["model"] = spec.model
        if spec.system_prompt is not None:
            thread_params["developerInstructions"] = spec.system_prompt
        thread_response = _as_mapping(
            transport.request("thread/start", thread_params), "thread/start"
        )
        thread = _as_mapping(thread_response.get("thread"), "thread/start")
        thread_id = _required_string(thread, "id", "thread/start")
        actual_model = _required_string(thread_response, "model", "thread/start")
        actual_cwd = _required_string(thread_response, "cwd", "thread/start")
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": spec.prompt}],
        }
        if spec.model is not None:
            turn_params["model"] = spec.model
        turn_response = _as_mapping(transport.request("turn/start", turn_params), "turn/start")
        turn = _as_mapping(turn_response.get("turn"), "turn/start")
        turn_id = _required_string(turn, "id", "turn/start")
        handle = RunHandle(
            turn_id,
            "codex",
            thread_id,
            actual_model,
            actual_cwd,
            "api" if spec.model is not None else "provider-default",
        )
        self._owned_sessions.add(thread_id)
        self._runs[handle.run_id] = _RunState(handle, turn_id, [self._run_started_event(handle)])
        debug_log("Started Codex run through app-server", "providers")
        return handle

    def stream(self, run: RunHandle) -> Iterator[RunEvent]:
        state = self._runs.get(run.run_id)
        if state is None or state.handle != run:
            raise ValueError("Unknown Codex run handle")
        while state.initial_events:
            yield state.initial_events.pop(0)
        while not state.terminal:
            try:
                if state.process is not None:
                    events = _normalise_exec_message(
                        state.process.next_message(STREAM_MESSAGE_TIMEOUT_SECONDS)
                    )
                else:
                    events = normalise_codex_message(self._next_run_message(state))
            except CodexTimeoutError:
                debug_log("Codex stream reached its message timeout", "providers")
                events = [
                    RunEvent(
                        "run.finished",
                        {"status": "timeout", "reason": "Codex stopped sending events"},
                    )
                ]
            except CodexProtocolError:
                events = [
                    RunEvent(
                        "run.finished",
                        {"status": "error", "reason": "Codex stream transport failed"},
                    )
                ]
            for event in events:
                if event.kind == "run.finished":
                    state.terminal = True
                yield event

    def steer(self, run: RunHandle, text: str) -> bool | NotSupported:
        if self._transport_state.mode == "exec-jsonl":
            return NotSupported("steer", "Codex exec JSONL does not support mid-run steering")
        state = self._runs.get(run.run_id)
        turn_id = state.turn_id if state is not None and state.turn_id is not None else run.run_id
        response = self._app_server().request(
            "turn/steer",
            {
                "threadId": run.session_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        return isinstance(response, dict) and isinstance(response.get("turnId"), str)

    def interrupt(self, run: RunHandle) -> bool | NotSupported:
        state = self._runs.get(run.run_id)
        if self._transport_state.mode == "exec-jsonl":
            return (
                state.process.interrupt()
                if state is not None and state.process is not None
                else False
            )
        turn_id = state.turn_id if state is not None and state.turn_id is not None else run.run_id
        response = self._app_server().request(
            "turn/interrupt", {"threadId": run.session_id, "turnId": turn_id}
        )
        return isinstance(response, dict)

    def resume(self, session_id: str) -> RunHandle | NotSupported:
        if self._transport_state.mode == "exec-jsonl":
            return NotSupported("resume", "Codex exec JSONL resume requires a new prompt")
        response = _as_mapping(
            self._app_server().request("thread/resume", {"threadId": session_id}),
            "thread/resume",
        )
        handle, turn_id = self._handle_from_thread_response(response)
        self._owned_sessions.add(handle.session_id)
        self._runs[handle.run_id] = _RunState(handle, turn_id, [])
        return handle

    def fork(self, session_id: str) -> RunHandle | NotSupported:
        if self._transport_state.mode == "exec-jsonl":
            return NotSupported("fork", "Codex exec JSONL fork requires a new prompt")
        response = _as_mapping(
            self._app_server().request("thread/fork", {"threadId": session_id}),
            "thread/fork",
        )
        handle, turn_id = self._handle_from_thread_response(response)
        self._owned_sessions.add(handle.session_id)
        self._runs[handle.run_id] = _RunState(handle, turn_id, [])
        return handle

    def list_sessions(self) -> list[SessionInfo] | NotSupported:
        if self._transport_state.mode == "exec-jsonl":
            return NotSupported("list_sessions", "Codex exec JSONL cannot enumerate sessions")
        sessions: list[SessionInfo] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"archived": False}
            if cursor is not None:
                params["cursor"] = cursor
            response = _as_mapping(
                self._app_server().request("thread/list", params), "thread/list"
            )
            data = response.get("data")
            if not isinstance(data, list):
                raise CodexProtocolError("Codex thread/list response has an invalid data field")
            for value in data:
                sessions.append(self._session_info(_as_mapping(value, "thread/list")))
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise CodexProtocolError("Codex thread/list returned an invalid nextCursor")
            cursor = next_cursor
        return sessions

    def _session_info(self, thread: Mapping[str, Any]) -> SessionInfo:
        session_id = _required_string(thread, "id", "thread/list")
        status_value = thread.get("status")
        status = status_value.get("type") if isinstance(status_value, dict) else status_value
        name, preview = thread.get("name"), thread.get("preview")
        return SessionInfo(
            session_id=session_id,
            provider="codex",
            owned=session_id in self._owned_sessions,
            title=name if isinstance(name, str) else preview if isinstance(preview, str) else None,
            model=thread.get("model") if isinstance(thread.get("model"), str) else None,
            cwd=thread.get("cwd") if isinstance(thread.get("cwd"), str) else None,
            status=status if isinstance(status, str) else None,
            started_at=_timestamp_from_unix(thread.get("createdAt")),
        )

    def usage(self) -> UsageSnapshot:
        if self._transport_state.mode == "exec-jsonl":
            return UsageSnapshot(
                detail="Codex usage is unavailable through the exec JSONL fallback"
            )
        try:
            transport = self._app_server()
            usage_response = transport.request("account/usage/read", {})
            rate_response = transport.request("account/rateLimits/read", {})
        except (CodexProtocolError, OSError, subprocess.SubprocessError):
            return UsageSnapshot(detail="Codex usage and rate-limit readings are unavailable")
        usage_valid = isinstance(usage_response, dict) and isinstance(
            usage_response.get("summary"), dict
        )
        if not isinstance(rate_response, dict) or not isinstance(
            rate_response.get("rateLimits"), dict
        ):
            return UsageSnapshot(detail="Codex usage and rate-limit readings are unavailable")
        primary = rate_response["rateLimits"].get("primary")
        used_value = primary.get("usedPercent") if isinstance(primary, dict) else None
        if not isinstance(used_value, (int, float)) or isinstance(used_value, bool):
            if usage_valid:
                return UsageSnapshot(
                    available=True, detail="Codex token usage summary is available"
                )
            return UsageSnapshot(
                detail="Codex usage and rate-limit readings are unavailable"
            )
        used_percent = float(used_value)
        return UsageSnapshot(
            available=True,
            limit=100.0,
            remaining=max(0.0, 100.0 - used_percent),
            resets_at=_timestamp_from_unix(primary.get("resetsAt")),
            detail="Codex primary rate-limit window",
        )

    def health(self) -> HealthReport:
        checked_at = datetime.now(timezone.utc).isoformat()
        if (
            self._transport_state.mode == "unavailable"
            and self._transport_state.detail != "Codex transport has not started"
        ):
            return HealthReport(
                reachable=False,
                detail=self._transport_state.detail,
                checked_at=checked_at,
            )
        if self._transport_state.mode == "exec-jsonl":
            return HealthReport(
                True,
                detail="Codex exec JSONL fallback is active because app-server failed to start",
                checked_at=checked_at,
            )
        started = self._clock()
        try:
            _as_mapping(
                self._app_server().request("account/read", {"refreshToken": False}),
                "account/read",
            )
        except (CodexProtocolError, OSError, subprocess.SubprocessError) as error:
            debug_log(f"Codex health check failed: {type(error).__name__}", "providers")
            return HealthReport(False, detail=self._transport_state.detail, checked_at=checked_at)
        return HealthReport(
            True,
            (self._clock() - started) * 1000.0,
            self._transport_state.detail,
            checked_at,
        )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        for state in self._runs.values():
            if state.process is not None:
                state.process.close()

    def _require_subscription(self, *, force_refresh: bool = False) -> AuthStatus:
        status = self._auth_manager.status("codex", force_refresh=force_refresh)
        if not status.logged_in or status.method != "chatgpt":
            debug_log("Codex run blocked because ChatGPT subscription auth is absent", "providers")
            raise PermissionError("Codex requires ChatGPT subscription authentication")
        return status

    def _app_server(self) -> _Transport:
        self._ensure_transport()
        if self._transport is None or self._transport_state.mode != "app-server":
            raise CodexProtocolError(self._transport_state.detail)
        return self._transport

    def _ensure_transport(self) -> None:
        if self._transport is not None:
            if self._transport_state.mode == "app-server" and not self._server_auth_verified:
                try:
                    self._verify_server_auth(self._transport)
                except (PermissionError, CodexProtocolError):
                    self._transport_state = CodexTransportState(
                        "unavailable",
                        "Codex app-server authentication verification failed",
                    )
                    raise
            return
        self._require_subscription()
        command = self._command or _resolve_codex_command()
        debug_log("Starting Codex app-server transport", "providers")
        try:
            transport = _JsonRpcTransport(
                command,
                environment=scrub_provider_environment(),
                request_timeout=self._request_timeout,
                process_factory=self._process_factory,
            )
        except (OSError, subprocess.SubprocessError, CodexProtocolError) as error:
            debug_log(
                f"Codex app-server failed to start: {type(error).__name__}; exec JSONL fallback selected",
                "providers",
            )
            self._transport_state = CodexTransportState(
                "exec-jsonl", "Codex app-server failed to start; exec JSONL fallback is active"
            )
            return
        try:
            self._verify_server_auth(transport)
        except (PermissionError, CodexProtocolError):
            self._transport_state = CodexTransportState(
                "unavailable", "Codex app-server authentication verification failed"
            )
            raise
        self._transport = transport
        self._transport_state = CodexTransportState(
            "app-server", "Codex app-server JSON-RPC transport is active"
        )

    def _verify_server_auth(self, transport: _Transport) -> None:
        try:
            response = _as_mapping(
                transport.request("account/read", {"refreshToken": False}), "account/read"
            )
        except Exception:
            transport.close()
            raise
        account = response.get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            transport.close()
            raise PermissionError("Codex app-server is not authenticated through ChatGPT")
        self._server_auth_verified = True
        debug_log("Verified Codex app-server ChatGPT authentication", "providers")

    def _start_exec_run(self, spec: RunSpec) -> RunHandle:
        if spec.system_prompt is not None or spec.allowed_tools or spec.skills or spec.toolsets:
            raise CodexProtocolError("Codex exec JSONL cannot represent all requested run fields")
        process = _ExecJsonProcess(
            self._command or _resolve_codex_command(),
            spec,
            environment=scrub_provider_environment(),
            process_factory=self._process_factory,
        )
        try:
            self._require_subscription(force_refresh=True)
            first = process.next_message(self._request_timeout)
        except Exception:
            process.close()
            self._transport_state = CodexTransportState(
                "unavailable",
                "Codex app-server and exec JSONL fallback could not start",
            )
            raise
        if first.get("type") != "thread.started" or not isinstance(first.get("thread_id"), str):
            process.close()
            raise CodexProtocolError("Codex exec did not report a thread.started event")
        session_id = first["thread_id"]
        run_id = str(uuid.uuid4())
        handle = RunHandle(
            run_id,
            "codex",
            session_id,
            spec.model,
            spec.cwd,
            "api" if spec.model is not None else "provider-default",
        )
        self._owned_sessions.add(session_id)
        self._runs[run_id] = _RunState(
            handle, None, [self._run_started_event(handle)], process=process
        )
        debug_log("Started Codex run through visible exec JSONL fallback", "providers")
        return handle

    def _validate_fallback_model(self, model: str | None) -> None:
        if model is not None and model not in self._catalogue_ids:
            raise ValueError(
                "The requested Codex model cannot be confirmed while app-server is unavailable"
            )

    def _run_started_event(self, handle: RunHandle) -> RunEvent:
        return RunEvent(
            "run.started",
            {
                "run_id": handle.run_id,
                "provider": "codex",
                "model": handle.model,
                "session_id": handle.session_id,
                "cwd": handle.cwd,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _next_run_message(self, state: _RunState) -> dict[str, Any]:
        for index, message in enumerate(self._deferred_messages):
            if _message_matches_run(message, state):
                return self._deferred_messages.pop(index)
        transport = self._app_server()
        while True:
            message = transport.next_message(STREAM_MESSAGE_TIMEOUT_SECONDS)
            if _message_matches_run(message, state):
                return message
            self._deferred_messages.append(message)

    def _handle_from_thread_response(
        self, response: Mapping[str, Any]
    ) -> tuple[RunHandle, str | None]:
        thread = _as_mapping(response.get("thread"), "thread response")
        thread_id = _required_string(thread, "id", "thread response")
        turns = thread.get("turns")
        active_turn_id: str | None = None
        if isinstance(turns, list):
            for value in reversed(turns):
                if (
                    isinstance(value, dict)
                    and value.get("status") == "inProgress"
                    and isinstance(value.get("id"), str)
                ):
                    active_turn_id = value["id"]
                    break
        model = response.get("model")
        if not isinstance(model, str):
            model = thread.get("model") if isinstance(thread.get("model"), str) else None
        cwd = response.get("cwd")
        if not isinstance(cwd, str):
            cwd = thread.get("cwd") if isinstance(thread.get("cwd"), str) else None
        handle = RunHandle(
            active_turn_id or thread_id,
            "codex",
            thread_id,
            model,
            cwd,
            "api" if model is not None else "unknown",
        )
        return handle, active_turn_id


def _message_matches_run(message: Mapping[str, Any], state: _RunState) -> bool:
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    thread_id = params.get("threadId")
    if thread_id is not None and thread_id != state.handle.session_id:
        return False
    turn_id = params.get("turnId")
    if turn_id is None and isinstance(params.get("turn"), dict):
        turn_id = params["turn"].get("id")
    return turn_id is None or state.turn_id is None or turn_id == state.turn_id
