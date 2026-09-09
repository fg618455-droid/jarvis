from __future__ import annotations

import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from jarvis.providers.base import AuthStatus, Capabilities, NotSupported, RunSpec
from jarvis.providers.codex import (
    CodexAdapter,
    CodexProtocolError,
    CodexTimeoutError,
    CodexTransportState,
    normalise_codex_message,
)


class StubAuthenticationManager:
    def __init__(self, status: AuthStatus | None = None) -> None:
        self._status = status or AuthStatus(logged_in=True, method="chatgpt")

    def status(self, provider: str, *, force_refresh: bool = False) -> AuthStatus:
        assert provider == "codex"
        return self._status


class StubTransport:
    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.messages = deque(messages or [])
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.requests.append((method, params))
        response = self.responses.get(method)
        if isinstance(response, Exception):
            raise response
        return response

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        if not self.messages:
            raise CodexProtocolError("Recorded stream ended before a terminal event")
        return self.messages.popleft()

    def close(self) -> None:
        return None


def app_server_responses() -> dict[str, Any]:
    return {
        "account/read": {
            "account": {"type": "chatgpt", "email": None, "planType": "plus"},
            "requiresOpenaiAuth": True,
        },
        "model/list": {
            "data": [
                {
                    "id": "catalogue-model",
                    "model": "catalogue-model",
                    "displayName": "Catalogue Model",
                }
            ],
            "nextCursor": None,
        },
        "modelProvider/capabilities/read": {
            "imageGeneration": True,
            "namespaceTools": True,
            "webSearch": False,
        },
    }


@pytest.mark.unit
def test_codex_identity_and_subscription_authentication_are_explicit() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=StubTransport(app_server_responses()),
    )

    assert adapter.id() == "codex"
    assert adapter.auth_status() == AuthStatus(logged_in=True, method="chatgpt")
    assert adapter.transport_state() == CodexTransportState(
        mode="app-server", detail="Codex app-server JSON-RPC transport is active"
    )


@pytest.mark.unit
def test_model_list_uses_only_api_entries() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=StubTransport(app_server_responses()),
    )

    catalogue = adapter.list_models()

    assert catalogue.enumerable is True
    assert [(model.id, model.display_name, model.source) for model in catalogue.models] == [
        ("catalogue-model", "Catalogue Model", "api")
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [{}, {"data": {}}, {"data": [{"id": "missing-model-field"}]}, {"data": [{"id": 1, "model": 1}]}],
)
def test_model_list_rejects_changed_response_shapes(response: dict[str, Any]) -> None:
    responses = app_server_responses()
    responses["model/list"] = response
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(), transport=StubTransport(responses)
    )

    with pytest.raises(CodexProtocolError, match="model/list"):
        adapter.list_models()


@pytest.mark.unit
def test_capabilities_are_derived_from_reported_flags_and_unknowns_are_false() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=StubTransport(app_server_responses()),
    )

    assert adapter.capabilities("catalogue-model") == Capabilities(
        tools=True,
        mcp=False,
        streaming=False,
        images=True,
        structured_output=False,
        steering=False,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile", "sandbox", "approval_policy"),
    [
        ("read_only", "read-only", "on-request"),
        ("project_dev", "workspace-write", "on-request"),
        ("automation", "workspace-write", "never"),
        ("unrestricted", "danger-full-access", "on-request"),
    ],
)
def test_start_run_translates_capability_profiles(
    profile: str, sandbox: str, approval_policy: str
) -> None:
    responses = app_server_responses()
    responses.update(
        {
            "thread/start": {
                "thread": {"id": "thread-1"},
                "model": "catalogue-model",
                "cwd": "C:/project",
            },
            "turn/start": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
        }
    )
    transport = StubTransport(responses)
    adapter = CodexAdapter(auth_manager=StubAuthenticationManager(), transport=transport)

    run = adapter.start_run(
        RunSpec(
            prompt="Hello",
            model="catalogue-model",
            cwd="C:/project",
            capability_profile=profile,
        )
    )

    thread_params = dict(transport.requests)["thread/start"]
    assert thread_params["sandbox"] == sandbox
    assert thread_params["approvalPolicy"] == approval_policy
    assert run.model == "catalogue-model"
    assert run.model_source == "api"
    assert run.session_id == "thread-1"
    assert run.run_id == "turn-1"


@pytest.mark.unit
def test_start_run_without_a_named_model_reports_provider_default() -> None:
    responses = app_server_responses()
    responses.update(
        {
            "thread/start": {
                "thread": {"id": "thread-1"},
                "model": "catalogue-model",
                "cwd": "C:/project",
            },
            "turn/start": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
        }
    )
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(), transport=StubTransport(responses)
    )

    run = adapter.start_run(RunSpec(prompt="Hello"))

    assert run.model == "catalogue-model"
    assert run.model_source == "provider-default"


@pytest.mark.unit
def test_api_key_authentication_cannot_start_a_run() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(AuthStatus(method="api_key")),
        transport=StubTransport(app_server_responses()),
    )

    with pytest.raises(PermissionError, match="ChatGPT subscription"):
        adapter.start_run(RunSpec(prompt="Hello"))


@pytest.mark.unit
def test_run_fields_that_are_not_wired_fail_visibly() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=StubTransport(app_server_responses()),
    )

    with pytest.raises(CodexProtocolError, match="mcp_config"):
        adapter.start_run(RunSpec(prompt="Hello", mcp_config="mcp.json"))


@pytest.mark.unit
def test_app_server_authentication_rejects_non_chatgpt_mode_after_start() -> None:
    responses = app_server_responses()
    responses["account/read"] = {
        "account": {"type": "apiKey"},
        "requiresOpenaiAuth": True,
    }
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(), transport=StubTransport(responses)
    )

    with pytest.raises(PermissionError, match="not authenticated through ChatGPT"):
        adapter.list_models()


@pytest.mark.unit
def test_app_server_start_failure_selects_a_visible_fallback() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        command=("codex-executable-that-does-not-exist",),
    )

    with pytest.raises(CodexProtocolError, match="exec JSONL fallback"):
        adapter.list_models()

    assert adapter.transport_state().mode == "exec-jsonl"


@pytest.mark.unit
def test_recorded_app_server_stream_normalises_to_the_contract() -> None:
    path = Path(__file__).parent / "golden" / "codex_app_server.jsonl"
    messages = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    events = [
        event
        for message in messages
        for event in normalise_codex_message(message)
    ]

    assert [event.kind for event in events] == [
        "turn.started",
        "thinking",
        "text.delta",
        "usage.delta",
        "run.finished",
    ]
    assert events[-1].payload == {"status": "ok", "reason": "Codex turn completed"}


@pytest.mark.unit
@pytest.mark.parametrize("code", ["usageLimitExceeded", "rateLimitExceeded"])
def test_quota_errors_terminate_as_quota(code: str) -> None:
    message = {
        "jsonrpc": "2.0",
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "items": [],
                "status": "failed",
                "error": {"message": "limit", "codexErrorInfo": code},
            },
        },
    }

    [event] = normalise_codex_message(message)

    assert event.kind == "run.finished"
    assert event.payload["status"] == "quota"


@pytest.mark.unit
def test_tool_arguments_are_redacted_from_events() -> None:
    message = {
        "jsonrpc": "2.0",
        "method": "item/started",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "startedAtMs": 1,
            "item": {
                "type": "mcpToolCall",
                "id": "tool-1",
                "server": "server",
                "tool": "lookup",
                "status": "inProgress",
                "arguments": {"secret": "must-not-escape"},
            },
        },
    }

    [event] = normalise_codex_message(message)

    assert event.payload == {
        "name": "server/lookup",
        "args_redacted": True,
        "tool_id": "tool-1",
    }
    assert "must-not-escape" not in repr(event.payload)


@pytest.mark.unit
def test_usage_reports_rate_limit_window_without_estimating_tokens() -> None:
    responses = app_server_responses()
    responses.update(
        {
            "account/usage/read": {"summary": {"lifetimeTokens": 1000}},
            "account/rateLimits/read": {
                "rateLimits": {
                    "planType": "plus",
                    "primary": {"usedPercent": 25, "resetsAt": 1789008054},
                    "secondary": None,
                }
            },
        }
    )
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(), transport=StubTransport(responses)
    )

    usage = adapter.usage()

    assert usage.available is True
    assert usage.limit == 100.0
    assert usage.remaining == 75.0
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.resets_at is not None


@pytest.mark.unit
def test_usage_is_explicitly_unavailable_for_a_changed_shape() -> None:
    responses = app_server_responses()
    responses.update({"account/usage/read": {}, "account/rateLimits/read": {}})
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(), transport=StubTransport(responses)
    )

    usage = adapter.usage()

    assert usage.available is False
    assert usage.detail == "Codex usage and rate-limit readings are unavailable"


@pytest.mark.unit
def test_sessions_resume_fork_steer_and_interrupt_use_supported_app_server_paths() -> None:
    responses = app_server_responses()
    thread_result = {
        "thread": {
            "id": "thread-2",
            "model": "catalogue-model",
            "cwd": "C:/project",
        },
        "model": "catalogue-model",
        "cwd": "C:/project",
    }
    responses.update(
        {
            "thread/list": {
                "data": [
                    {
                        "id": "external-thread",
                        "model": "catalogue-model",
                        "cwd": "C:/other",
                        "preview": "External",
                        "status": {"type": "idle"},
                        "createdAt": 1788990000,
                    }
                ],
                "nextCursor": None,
            },
            "thread/resume": thread_result,
            "thread/fork": thread_result,
            "turn/steer": {"turnId": "turn-1"},
            "turn/interrupt": {},
        }
    )
    transport = StubTransport(responses)
    adapter = CodexAdapter(auth_manager=StubAuthenticationManager(), transport=transport)

    resumed = adapter.resume("thread-1")
    forked = adapter.fork("thread-1")
    sessions = adapter.list_sessions()

    assert resumed.session_id == "thread-2"
    assert forked.session_id == "thread-2"
    assert sessions[0].owned is False
    assert adapter.steer(resumed, "More") is True
    assert adapter.interrupt(resumed) is True


@pytest.mark.unit
def test_fallback_state_is_visible_and_unsupported_paths_are_not_simulated() -> None:
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=StubTransport(app_server_responses()),
        transport_state=CodexTransportState(
            mode="exec-jsonl", detail="Codex app-server failed to start; exec JSONL fallback is active"
        ),
    )
    run = adapter.resume("thread-1")

    assert adapter.health().detail.startswith("Codex exec JSONL fallback")
    assert isinstance(run, NotSupported)
    assert run.capability == "resume"


@pytest.mark.unit
def test_adapter_child_process_receives_no_billing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = """import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'initialize':
        result = {'codexHome': '.', 'platformFamily': 'windows', 'platformOs': 'windows', 'userAgent': 'test'}
    elif method == 'account/read':
        result = {'account': {'type': 'chatgpt', 'email': None, 'planType': 'plus'}, 'requiresOpenaiAuth': True}
    elif method == 'model/list':
        clean = 'clean' if 'ANTHROPIC_API_KEY' not in os.environ and 'OPENAI_API_KEY' not in os.environ else 'leaked'
        result = {'data': [{'id': clean, 'model': clean, 'displayName': clean}], 'nextCursor': None}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        command=(sys.executable, "-c", helper),
        request_timeout=3.0,
    )

    catalogue = adapter.list_models()

    assert [model.id for model in catalogue.models] == ["clean"]
    adapter.close()


class TimingOutTransport(StubTransport):
    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        raise CodexTimeoutError("Codex app-server stream timed out")


@pytest.mark.unit
def test_a_silent_stream_terminates_as_timeout_rather_than_error() -> None:
    responses = app_server_responses()
    responses["thread/resume"] = {
        "thread": {
            "id": "thread-1",
            "turns": [{"id": "turn-1", "status": "inProgress"}],
        },
        "model": "catalogue-model",
        "cwd": ".",
    }
    adapter = CodexAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=TimingOutTransport(responses),
    )
    run = adapter.resume("thread-1")
    assert not isinstance(run, NotSupported)

    events = list(adapter.stream(run))

    assert events[-1].kind == "run.finished"
    assert events[-1].payload["status"] == "timeout"
