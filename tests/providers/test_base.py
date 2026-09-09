from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import get_type_hints

import pytest

from jarvis.providers.base import (
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
from jarvis.providers.models import ModelCatalog


@pytest.mark.unit
def test_provider_adapter_cannot_be_instantiated_without_the_full_contract():
    with pytest.raises(TypeError):
        ProviderAdapter()


@pytest.mark.unit
def test_provider_adapter_declares_the_run_oriented_contract():
    assert ProviderAdapter.__abstractmethods__ == {
        "auth_status",
        "capabilities",
        "fork",
        "health",
        "id",
        "interrupt",
        "list_models",
        "list_sessions",
        "resume",
        "start_run",
        "steer",
        "stream",
        "usage",
    }


@pytest.mark.unit
def test_status_and_capability_defaults_fail_closed():
    assert AuthStatus().logged_in is False
    assert Capabilities() == Capabilities(
        tools=False,
        mcp=False,
        streaming=False,
        images=False,
        structured_output=False,
        steering=False,
    )
    assert UsageSnapshot().available is False
    assert HealthReport().reachable is False


@pytest.mark.unit
def test_contract_values_are_immutable():
    run = RunHandle(
        run_id="run-1",
        provider="codex",
        session_id="session-1",
        model="model-1",
        cwd="C:/project",
    )

    with pytest.raises(FrozenInstanceError):
        run.model = "model-2"


@pytest.mark.unit
def test_run_spec_and_session_info_do_not_share_mutable_values():
    first = RunSpec(prompt="first")
    second = RunSpec(prompt="second")

    assert first.allowed_tools == ()
    assert second.allowed_tools == ()
    assert first.capability_profile == "read_only"
    assert first.mcp_config is None
    assert "permission_mode" not in {item.name for item in fields(RunSpec)}
    assert SessionInfo(session_id="external", provider="claude").owned is False


@pytest.mark.unit
def test_run_spec_rejects_unknown_capability_profiles():
    with pytest.raises(ValueError, match="owner_mode"):
        RunSpec(prompt="hello", capability_profile="owner_mode")


@pytest.mark.unit
def test_model_catalog_explicitly_reports_whether_it_can_be_enumerated():
    claude_catalogue = ModelCatalog(enumerable=False)

    assert claude_catalogue.enumerable is False
    assert claude_catalogue.models == ()
    assert get_type_hints(ProviderAdapter.list_models)["return"] is ModelCatalog


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (
            "run.started",
            {
                "run_id": "run-1",
                "provider": "claude",
                "model": None,
                "session_id": "session-1",
                "cwd": "C:/project",
                "ts": "2026-09-09T10:00:00+00:00",
            },
        ),
        ("turn.started", {"turn_id": "turn-1"}),
        ("text.delta", {"text": "Hello"}),
        ("thinking", {"text": "Considering"}),
        (
            "tool.call",
            {"name": "read_file", "args_redacted": {}, "tool_id": "tool-1"},
        ),
        (
            "tool.result",
            {"tool_id": "tool-1", "ok": True, "summary": "Read", "bytes": 12},
        ),
        (
            "approval.needed",
            {"kind": "write", "detail": "Write file", "options": ["allow", "deny"]},
        ),
        ("needs_you", {"question": "Which file?"}),
        ("usage.delta", {"input": 3, "output": 1, "cached": 0}),
        ("run.finished", {"status": "quota", "reason": "Limit reached"}),
    ],
)
def test_run_event_accepts_the_normalised_event_contract(kind, payload):
    assert RunEvent(kind=kind, payload=payload).payload == payload


@pytest.mark.unit
def test_run_event_rejects_unknown_event_kinds():
    with pytest.raises(ValueError, match="Unsupported provider event"):
        RunEvent(kind="provider.switched", payload={})


@pytest.mark.unit
def test_run_event_rejects_missing_contract_fields():
    with pytest.raises(ValueError, match="text"):
        RunEvent(kind="text.delta", payload={})


@pytest.mark.unit
def test_run_finished_rejects_non_terminal_statuses():
    with pytest.raises(ValueError, match="running"):
        RunEvent(kind="run.finished", payload={"status": "running", "reason": None})


@pytest.mark.unit
def test_model_sources_are_restricted_to_verified_contract_values():
    assert ModelInfo(id="model-1", source="api").source == "api"

    with pytest.raises(ValueError, match="guessed"):
        ModelInfo(id="model-2", source="guessed")


@pytest.mark.unit
def test_not_supported_is_an_explicit_result():
    result = NotSupported(capability="fork", reason="Provider cannot fork sessions")

    assert result.supported is False
    assert result.capability == "fork"
