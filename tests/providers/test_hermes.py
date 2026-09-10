from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.providers.base import AuthStatus, NotSupported, RunSpec
from jarvis.providers.hermes import (
    HermesAdapter,
    HermesProtocolError,
    HermesTimeoutError,
    HermesTransportState,
    normalise_hermes_message,
)


GOLDEN = Path(__file__).parent / "golden" / "hermes_acp.jsonl"


def golden_messages() -> list[dict[str, Any]]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]


class StubAuthenticationManager:
    def __init__(self, status: AuthStatus | None = None) -> None:
        self._status = status or AuthStatus(
            logged_in=True, method="chatgpt", plan="openai-codex"
        )

    def status(self, provider: str, *, force_refresh: bool = False) -> AuthStatus:
        assert provider == "hermes"
        return self._status


class StubTransport:
    def __init__(
        self,
        initialize: dict[str, Any],
        session: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.initialize = initialize
        self.session = session or {}
        self.messages = list(messages or [])
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int, dict[str, Any]]] = []

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.requests.append((method, params))
        if method == "initialize":
            return self.initialize
        if method in {"session/new", "session/fork"}:
            return self.session
        if method == "session/list":
            return {"sessions": [], "nextCursor": None}
        return {}

    def begin_request(self, method: str, params: dict[str, Any]) -> int:
        self.requests.append((method, params))
        return 3

    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        if not self.messages:
            raise HermesProtocolError("Hermes ACP stream closed")
        return self.messages.pop(0)

    def respond(self, request_id: int, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    def close(self) -> None:
        pass


def recorded_transport() -> StubTransport:
    messages = golden_messages()
    return StubTransport(
        messages[0]["result"],
        messages[1]["result"],
        messages[2:],
    )


def adapter_with(transport: StubTransport, **kwargs: Any) -> HermesAdapter:
    return HermesAdapter(
        auth_manager=StubAuthenticationManager(),
        transport=transport,
        **kwargs,
    )


@pytest.mark.unit
def test_identity_and_subscription_authentication_are_explicit() -> None:
    adapter = adapter_with(recorded_transport())

    assert adapter.id() == "hermes"
    assert adapter.auth_status() == AuthStatus(
        logged_in=True, method="chatgpt", plan="openai-codex"
    )


@pytest.mark.unit
def test_the_recorded_acp_run_normalises_onto_the_contract() -> None:
    adapter = adapter_with(recorded_transport())

    run = adapter.start_run(RunSpec(prompt="Reply with exactly the word: ping"))
    events = list(adapter.stream(run))

    assert [event.kind for event in events] == [
        "run.started",
        "turn.started",
        "text.delta",
        "usage.delta",
        "run.finished",
    ]
    assert events[0].payload["model"] == "openai-codex:gpt-5.6-terra"
    assert events[2].payload["text"] == "ping"
    assert events[3].payload == {"input": 15508, "output": 5, "cached": 0}
    assert events[-1].payload == {
        "status": "ok",
        "reason": "Hermes finished the turn",
    }


@pytest.mark.unit
def test_capabilities_come_only_from_the_acp_handshake() -> None:
    adapter = adapter_with(recorded_transport())

    assert adapter.capabilities(None).streaming is False
    adapter.health()
    capabilities = adapter.capabilities(None)

    assert capabilities.images is True
    assert capabilities.streaming is True
    assert capabilities.tools is False
    assert capabilities.mcp is False
    assert capabilities.structured_output is False
    assert capabilities.steering is False


@pytest.mark.unit
def test_the_catalogue_comes_from_what_acp_advertises() -> None:
    adapter = adapter_with(recorded_transport())

    catalogue = adapter.list_models()

    assert catalogue.enumerable is True
    assert len(catalogue.models) == 143
    assert all(model.source == "api" for model in catalogue.models)
    assert catalogue.models[0].id == "openrouter:anthropic/claude-fable-5.1"


@pytest.mark.unit
@pytest.mark.parametrize(
    "models",
    [
        {"currentModelId": "openai-codex:gpt-5.6-terra"},
        {"availableModels": {}, "currentModelId": "openai-codex:gpt-5.6-terra"},
        {"availableModels": [{}], "currentModelId": "openai-codex:gpt-5.6-terra"},
        {"availableModels": [{"modelId": 1}], "currentModelId": "x"},
    ],
)
def test_a_changed_model_advertisement_fails_visibly(models: dict[str, Any]) -> None:
    messages = golden_messages()
    session = dict(messages[1]["result"])
    session["models"] = models
    adapter = adapter_with(StubTransport(messages[0]["result"], session, messages[2:]))

    with pytest.raises(HermesProtocolError):
        adapter.list_models()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile", "mode"),
    [
        ("read_only", "default"),
        ("project_dev", "accept_edits"),
        ("automation", "dont_ask"),
    ],
)
def test_representable_profiles_select_an_advertised_acp_mode(
    profile: str, mode: str
) -> None:
    transport = recorded_transport()
    adapter = adapter_with(transport)

    adapter.start_run(RunSpec(prompt="ping", capability_profile=profile))

    assert ("session/set_mode", {"sessionId": transport.session["sessionId"], "modeId": mode}) in transport.requests


@pytest.mark.unit
def test_unrestricted_is_rejected_because_acp_cannot_represent_it_faithfully() -> None:
    adapter = adapter_with(recorded_transport())

    with pytest.raises(HermesProtocolError, match="unrestricted"):
        adapter.start_run(RunSpec(prompt="ping", capability_profile="unrestricted"))


@pytest.mark.unit
def test_requested_model_is_pinned_only_when_acp_advertises_it() -> None:
    transport = recorded_transport()
    adapter = adapter_with(transport)
    model = "openai-codex:gpt-5.6-sol"

    run = adapter.start_run(RunSpec(prompt="ping", model=model))

    assert run.model == model
    assert run.model_source == "api"
    assert ("session/set_model", {"sessionId": run.session_id, "modelId": model}) in transport.requests


@pytest.mark.unit
def test_unadvertised_control_paths_return_not_supported() -> None:
    initialize = {
        "protocolVersion": 1,
        "agentCapabilities": {"sessionCapabilities": {}},
    }
    transport = StubTransport(initialize, {"sessionId": "s", "models": {}, "modes": {}})
    adapter = adapter_with(transport)
    adapter.health()
    handle = adapter.resume("session")

    assert isinstance(handle, NotSupported)
    assert isinstance(adapter.fork("session"), NotSupported)
    assert isinstance(adapter.list_sessions(), NotSupported)
    assert isinstance(adapter.steer(run=type("Run", (), {})(), text="more"), NotSupported)


@pytest.mark.unit
def test_interrupt_uses_acp_cancel_without_simulating_steering() -> None:
    transport = recorded_transport()
    adapter = adapter_with(transport)
    run = adapter.start_run(RunSpec(prompt="ping"))

    assert isinstance(adapter.steer(run, "more"), NotSupported)
    assert adapter.interrupt(run) is True
    assert ("session/cancel", {"sessionId": run.session_id}) in transport.requests


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stop_reason", "status"),
    [("end_turn", "ok"), ("cancelled", "cancelled"), ("quota", "error"), ("refusal", "error")],
)
def test_prompt_stop_reasons_have_honest_terminal_states(
    stop_reason: str, status: str
) -> None:
    events = normalise_hermes_message(
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": stop_reason}},
        prompt_request_id=3,
    )

    assert events[-1].kind == "run.finished"
    assert events[-1].payload["status"] == status
    assert events[-1].payload["status"] != "quota"


class TimingOutTransport(StubTransport):
    def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        raise HermesTimeoutError("Hermes ACP stream timed out")


@pytest.mark.unit
def test_timeout_and_transport_failure_remain_distinct_terminal_states() -> None:
    golden = golden_messages()
    timeout_adapter = adapter_with(
        TimingOutTransport(golden[0]["result"], golden[1]["result"])
    )
    timeout_run = timeout_adapter.start_run(RunSpec(prompt="ping"))
    timeout_events = list(timeout_adapter.stream(timeout_run))

    error_transport = StubTransport(golden[0]["result"], golden[1]["result"])
    error_adapter = adapter_with(error_transport)
    error_run = error_adapter.start_run(RunSpec(prompt="ping"))
    error_events = list(error_adapter.stream(error_run))

    assert timeout_events[-1].payload["status"] == "timeout"
    assert error_events[-1].payload["status"] == "error"


@pytest.mark.unit
def test_usage_is_unavailable_before_a_run_and_records_real_acp_counts() -> None:
    adapter = adapter_with(recorded_transport())

    assert adapter.usage().available is False
    run = adapter.start_run(RunSpec(prompt="ping"))
    list(adapter.stream(run))
    usage = adapter.usage()

    assert usage.available is True
    assert usage.input_tokens == 15508
    assert usage.output_tokens == 5
    assert usage.cached_tokens == 0
    assert usage.limit is None
    assert usage.remaining is None
    assert "no quota window of its own" in (usage.detail or "")


@pytest.mark.unit
def test_permission_requests_are_visible_and_fail_closed() -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 41,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s",
            "options": [
                {"optionId": "allow_once", "name": "Allow once"},
                {"optionId": "cancel", "name": "Cancel"},
            ],
            "toolCall": {"toolCallId": "tool-1", "title": "Run command", "rawInput": {"secret": "hidden"}},
        },
    }

    [event] = normalise_hermes_message(request, prompt_request_id=3)

    assert event.kind == "approval.needed"
    assert event.payload["options"] == ["allow_once", "cancel"]
    assert "hidden" not in json.dumps(event.payload)


@pytest.mark.unit
def test_the_child_process_never_sees_billing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = r'''import json, os, sys
clean = not any(name in os.environ for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"))
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {"promptCapabilities": {"image": True}, "sessionCapabilities": {}} , "agentInfo": {"name": "clean" if clean else "leaked", "version": "test"}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    adapter = HermesAdapter(
        auth_manager=StubAuthenticationManager(),
        command=(sys.executable, "-c", helper),
        request_timeout=3.0,
    )

    report = adapter.health()

    assert report.reachable is True
    assert "clean" in (report.detail or "")
    adapter.close()


@pytest.mark.live
@pytest.mark.skip(reason="requires the installed Hermes provider and subscription")
def test_live_hermes_prompt() -> None:
    adapter = HermesAdapter()
    run = adapter.start_run(RunSpec(prompt="Reply with exactly the word: ping"))
    assert list(adapter.stream(run))[-1].payload["status"] == "ok"
    adapter.close()
