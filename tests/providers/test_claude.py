from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.providers.base import AuthStatus, NotSupported, RunSpec
from jarvis.providers.claude import (
    ClaudeAdapter,
    ClaudeProcessError,
    ClaudeTimeoutError,
    normalise_claude_message,
)


GOLDEN = Path(__file__).parent / "golden" / "claude_stream_json.jsonl"


def golden_messages() -> list[dict[str, Any]]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]


class StubAuthenticationManager:
    def __init__(self, status: AuthStatus | None = None) -> None:
        self._status = status or AuthStatus(logged_in=True, method="claude.ai", plan="pro")

    def status(self, provider: str, *, force_refresh: bool = False) -> AuthStatus:
        assert provider == "claude"
        return self._status


class StubSession:
    """A recorded Claude session that replays messages and records steering."""

    def __init__(self, messages: list[dict[str, Any]], *, timeout_after: int | None = None) -> None:
        self._messages = list(messages)
        self._timeout_after = timeout_after
        self._delivered = 0
        self.steered: list[str] = []
        self.interrupted = False
        self.closed = False

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        if self._timeout_after is not None and self._delivered >= self._timeout_after:
            raise ClaudeTimeoutError("Claude stopped sending events")
        if not self._messages:
            raise ClaudeProcessError("Claude stream ended before a result event")
        self._delivered += 1
        return self._messages.pop(0)

    def send_user_message(self, text: str) -> bool:
        self.steered.append(text)
        return True

    def interrupt(self) -> bool:
        self.interrupted = True
        return True

    def close(self) -> None:
        self.closed = True


def adapter_with(session: StubSession, **kwargs: Any) -> ClaudeAdapter:
    return ClaudeAdapter(
        auth_manager=StubAuthenticationManager(),
        session_factory=lambda command, spec, environment: session,
        **kwargs,
    )


@pytest.mark.unit
def test_identity_and_subscription_authentication_are_explicit() -> None:
    adapter = adapter_with(StubSession([]))

    assert adapter.id() == "claude"
    assert adapter.auth_status() == AuthStatus(logged_in=True, method="claude.ai", plan="pro")


@pytest.mark.unit
def test_an_api_key_login_cannot_start_a_run() -> None:
    adapter = ClaudeAdapter(
        auth_manager=StubAuthenticationManager(AuthStatus(logged_in=True, method="apiKey")),
        session_factory=lambda command, spec, environment: StubSession([]),
    )

    with pytest.raises(PermissionError):
        adapter.start_run(RunSpec(prompt="ping"))


@pytest.mark.unit
def test_the_recorded_stream_normalises_onto_the_contract() -> None:
    session = StubSession(golden_messages())
    adapter = adapter_with(session)

    run = adapter.start_run(RunSpec(prompt="Reply with exactly the word: ping"))
    events = list(adapter.stream(run))

    assert [event.kind for event in events] == [
        "run.started",
        "turn.started",
        "text.delta",
        "usage.delta",
        "run.finished",
    ]
    assert events[0].payload["model"] == "claude-opus-5"
    assert events[2].payload["text"] == "ping"
    assert events[-1].payload["status"] == "ok"


@pytest.mark.unit
def test_the_model_is_learned_from_the_run_rather_than_guessed() -> None:
    adapter = adapter_with(StubSession(golden_messages()))

    empty = adapter.list_models()
    assert empty.enumerable is False
    assert empty.models == ()

    run = adapter.start_run(RunSpec(prompt="ping"))
    list(adapter.stream(run))

    learned = adapter.list_models()
    assert learned.enumerable is False
    assert [(model.id, model.source) for model in learned.models] == [
        ("claude-opus-5", "provider-default")
    ]


@pytest.mark.unit
def test_a_caller_selected_model_is_recorded_as_a_verified_probe() -> None:
    messages = golden_messages()
    adapter = adapter_with(StubSession(messages))

    run = adapter.start_run(RunSpec(prompt="ping", model="claude-opus-5"))
    list(adapter.stream(run))

    assert run.model_source == "verified-probe"
    assert [model.source for model in adapter.list_models().models] == ["verified-probe"]


@pytest.mark.unit
def test_capabilities_come_from_the_init_event_and_start_closed() -> None:
    adapter = adapter_with(StubSession(golden_messages()))

    assert adapter.capabilities(None) == adapter.capabilities("claude-opus-5")
    assert adapter.capabilities(None).tools is False

    run = adapter.start_run(RunSpec(prompt="ping"))
    list(adapter.stream(run))

    learned = adapter.capabilities(None)
    assert learned.tools is True
    assert learned.streaming is True
    assert learned.steering is True
    assert learned.structured_output is True
    assert learned.mcp is False


@pytest.mark.unit
def test_rate_limit_events_become_a_usage_snapshot() -> None:
    adapter = adapter_with(StubSession(golden_messages()))

    assert adapter.usage().available is False

    run = adapter.start_run(RunSpec(prompt="ping"))
    list(adapter.stream(run))

    snapshot = adapter.usage()
    assert snapshot.available is True
    assert snapshot.limit == 100.0
    assert snapshot.remaining == pytest.approx(9.0)
    assert snapshot.resets_at == "2026-09-10T19:00:00+00:00"
    assert snapshot.input_tokens == 2
    assert snapshot.output_tokens == 4


@pytest.mark.unit
def test_a_blocked_rate_limit_terminates_the_run_as_quota() -> None:
    messages = [
        message
        for message in golden_messages()
        if message.get("type") in {"system", "rate_limit_event"}
    ]
    for message in messages:
        if message.get("type") == "rate_limit_event":
            message["rate_limit_info"]["status"] = "rejected"
            message["rate_limit_info"]["utilization"] = 1.0
    adapter = adapter_with(StubSession(messages))

    run = adapter.start_run(RunSpec(prompt="ping"))
    events = list(adapter.stream(run))

    assert events[-1].kind == "run.finished"
    assert events[-1].payload["status"] == "quota"


@pytest.mark.unit
def test_a_silent_stream_terminates_as_timeout_rather_than_error() -> None:
    adapter = adapter_with(StubSession(golden_messages(), timeout_after=1))

    run = adapter.start_run(RunSpec(prompt="ping"))
    events = list(adapter.stream(run))

    assert events[-1].kind == "run.finished"
    assert events[-1].payload["status"] == "timeout"


@pytest.mark.unit
def test_a_truncated_stream_terminates_as_error() -> None:
    adapter = adapter_with(StubSession(golden_messages()[:2]))

    run = adapter.start_run(RunSpec(prompt="ping"))
    events = list(adapter.stream(run))

    assert events[-1].kind == "run.finished"
    assert events[-1].payload["status"] == "error"


@pytest.mark.unit
def test_tool_arguments_never_reach_an_event() -> None:
    message = {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "Bash",
                "input": {"command": "cat ~/.ssh/id_rsa"},
            },
        },
    }

    [event] = normalise_claude_message(message)

    assert event.kind == "tool.call"
    assert event.payload == {"name": "Bash", "args_redacted": True, "tool_id": "toolu_01"}
    assert "id_rsa" not in json.dumps(event.payload)


@pytest.mark.unit
def test_a_denied_permission_becomes_an_approval_request() -> None:
    message = {
        "type": "system",
        "subtype": "permission_request",
        "tool_name": "Bash",
        "tool_use_id": "toolu_02",
    }

    [event] = normalise_claude_message(message)

    assert event.kind == "approval.needed"
    assert event.payload["options"] == ["approve", "decline"]


@pytest.mark.unit
def test_steering_and_interrupt_reach_the_session() -> None:
    session = StubSession(golden_messages())
    adapter = adapter_with(session)

    run = adapter.start_run(RunSpec(prompt="ping"))

    assert adapter.steer(run, "and again") is True
    assert adapter.interrupt(run) is True
    assert session.steered == ["and again"]
    assert session.interrupted is True


@pytest.mark.unit
def test_sessions_are_listed_with_ownership() -> None:
    listing = json.dumps(
        [
            {
                "id": "a86e85dc",
                "sessionId": "a86e85dc-3b84-4a47-a8e3-f5d905fe5274",
                "cwd": "/workspace",
                "kind": "background",
                "name": "Some other work",
                "state": "blocked",
                "startedAt": 1788477857404,
            }
        ]
    )
    adapter = ClaudeAdapter(
        auth_manager=StubAuthenticationManager(),
        session_factory=lambda command, spec, environment: StubSession([]),
        runner=lambda command, **kwargs: _completed(listing),
    )

    sessions = adapter.list_sessions()

    assert not isinstance(sessions, NotSupported)
    assert sessions[0].session_id == "a86e85dc-3b84-4a47-a8e3-f5d905fe5274"
    assert sessions[0].owned is False
    assert sessions[0].status == "blocked"


@pytest.mark.unit
def test_a_changed_session_listing_is_reported_rather_than_guessed() -> None:
    adapter = ClaudeAdapter(
        auth_manager=StubAuthenticationManager(),
        session_factory=lambda command, spec, environment: StubSession([]),
        runner=lambda command, **kwargs: _completed("not json at all"),
    )

    with pytest.raises(ClaudeProcessError):
        adapter.list_sessions()


@pytest.mark.unit
def test_the_child_process_never_sees_billing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = """import json, os, sys
keys = [name for name in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY') if name in os.environ]
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's', 'cwd': '.',
                  'model': 'leaked' if keys else 'clean', 'tools': [], 'mcp_servers': [],
                  'permissionMode': 'plan', 'apiKeySource': 'none', 'capabilities': []}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's',
                  'stop_reason': 'end_turn', 'result': 'ok', 'api_error_status': None,
                  'usage': {'input_tokens': 1, 'output_tokens': 1,
                            'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}}), flush=True)
"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    adapter = ClaudeAdapter(
        auth_manager=StubAuthenticationManager(),
        command=(sys.executable, "-c", helper),
    )

    run = adapter.start_run(RunSpec(prompt="ping"))
    events = list(adapter.stream(run))

    assert events[0].payload["model"] == "clean"
    adapter.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile", "mode"),
    [
        ("read_only", "plan"),
        ("project_dev", "acceptEdits"),
        ("automation", "dontAsk"),
        ("unrestricted", "bypassPermissions"),
    ],
)
def test_capability_profiles_map_to_native_permission_modes(profile: str, mode: str) -> None:
    from jarvis.providers.claude import permission_mode_for

    assert permission_mode_for(profile) == mode


class _Completed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0
        self.args: list[str] = []


def _completed(stdout: str) -> _Completed:
    return _Completed(stdout)
