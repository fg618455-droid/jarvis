"""One contract every adapter's recorded stream has to satisfy.

The per-adapter suites check what each provider means. This one checks what
they must agree on, so a fourth adapter cannot quietly invent its own shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

from jarvis.providers.base import (
    EVENT_REQUIRED_FIELDS,
    RUN_STATUSES,
    RunEvent,
)
from jarvis.providers.claude import normalise_claude_message
from jarvis.providers.codex import normalise_codex_message
from jarvis.providers.hermes import normalise_hermes_message


GOLDEN = Path(__file__).parent / "golden"
SECRETS = ("id_rsa", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "Bearer ", "sk-")


def _codex_events(messages: Iterable[dict[str, Any]]) -> list[RunEvent]:
    return [event for message in messages for event in normalise_codex_message(message)]


def _claude_events(messages: Iterable[dict[str, Any]]) -> list[RunEvent]:
    return [event for message in messages for event in normalise_claude_message(message)]


def _hermes_events(messages: Iterable[dict[str, Any]]) -> list[RunEvent]:
    prompt_request_id = 3
    return [
        event
        for message in messages
        for event in normalise_hermes_message(message, prompt_request_id=prompt_request_id)
    ]


ADAPTERS: list[tuple[str, str, Callable[[Iterable[dict[str, Any]]], list[RunEvent]]]] = [
    ("codex", "codex_app_server.jsonl", _codex_events),
    ("claude", "claude_stream_json.jsonl", _claude_events),
    ("hermes", "hermes_acp.jsonl", _hermes_events),
]


def load(name: str) -> list[dict[str, Any]]:
    path = GOLDEN / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "filename", "normalise"), ADAPTERS)
def test_every_recording_yields_contract_events(
    provider: str, filename: str, normalise: Callable[..., list[RunEvent]]
) -> None:
    """Every event an adapter emits is a kind the contract knows."""

    events = normalise(load(filename))

    assert events, f"{provider} produced no events from its recording"
    for event in events:
        assert event.kind in EVENT_REQUIRED_FIELDS
        assert EVENT_REQUIRED_FIELDS[event.kind].issubset(event.payload)


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "filename", "normalise"), ADAPTERS)
def test_every_recording_reports_the_assistant_text(
    provider: str, filename: str, normalise: Callable[..., list[RunEvent]]
) -> None:
    """The answer the provider gave reaches the caller as text."""

    events = normalise(load(filename))
    spoken = "".join(
        event.payload["text"] for event in events if event.kind == "text.delta"
    )
    raw = " ".join(json.dumps(message) for message in load(filename))

    assert spoken.strip(), f"{provider} dropped the assistant answer"
    assert json.dumps(spoken)[1:-1] in raw


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "filename", "normalise"), ADAPTERS)
def test_a_terminal_status_is_one_of_the_five(
    provider: str, filename: str, normalise: Callable[..., list[RunEvent]]
) -> None:
    """No adapter invents a sixth terminal state."""

    for event in normalise(load(filename)):
        if event.kind == "run.finished":
            assert event.payload["status"] in RUN_STATUSES


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "filename", "normalise"), ADAPTERS)
def test_no_recording_leaks_provider_payloads(
    provider: str, filename: str, normalise: Callable[..., list[RunEvent]]
) -> None:
    """Normalised events carry summaries, never raw provider content."""

    serialised = json.dumps(
        [{"kind": event.kind, "payload": event.payload} for event in normalise(load(filename))]
    )

    for secret in SECRETS:
        assert secret not in serialised


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "filename", "normalise"), ADAPTERS)
def test_tool_calls_never_carry_their_arguments(
    provider: str, filename: str, normalise: Callable[..., list[RunEvent]]
) -> None:
    """A tool call announces itself and redacts what it was asked to do."""

    for event in normalise(load(filename)):
        if event.kind == "tool.call":
            assert event.payload["args_redacted"] is True
            assert set(event.payload) == EVENT_REQUIRED_FIELDS["tool.call"]


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "filename", "normalise"), ADAPTERS)
def test_usage_counts_are_never_negative(
    provider: str, filename: str, normalise: Callable[..., list[RunEvent]]
) -> None:
    """A usage reading is a count, so it cannot be below zero."""

    for event in normalise(load(filename)):
        if event.kind == "usage.delta":
            for field in ("input", "output", "cached"):
                assert isinstance(event.payload[field], int)
                assert event.payload[field] >= 0


@pytest.mark.unit
def test_every_adapter_has_a_recording() -> None:
    """A new adapter cannot ship without a real recorded stream."""

    recorded = {filename for _, filename, _ in ADAPTERS}
    on_disk = {path.name for path in GOLDEN.glob("*.jsonl")}

    assert on_disk == recorded


TOOL_CALLS_WITH_SECRETS: list[tuple[str, Callable[[], list[RunEvent]]]] = [
    (
        "codex",
        lambda: normalise_codex_message(
            {
                "method": "item/started",
                "params": {
                    "threadId": "t",
                    "item": {
                        "id": "item_1",
                        "type": "commandExecution",
                        "command": "cat ~/.ssh/id_rsa",
                        "aggregatedOutput": "sk-secret-value",
                    },
                },
            }
        ),
    ),
    (
        "claude",
        lambda: normalise_claude_message(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "cat ~/.ssh/id_rsa", "token": "sk-secret-value"},
                    },
                },
            }
        ),
    ),
    (
        "hermes",
        lambda: normalise_hermes_message(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call_1",
                        "title": "Bash",
                        "rawInput": {"command": "cat ~/.ssh/id_rsa", "token": "sk-secret-value"},
                    },
                },
            },
            prompt_request_id=None,
        ),
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(("provider", "normalise"), TOOL_CALLS_WITH_SECRETS)
def test_a_tool_call_carrying_secrets_still_redacts_them(
    provider: str, normalise: Callable[[], list[RunEvent]]
) -> None:
    """Give each adapter a tool call full of secrets and read what escapes."""

    events = normalise()
    calls = [event for event in events if event.kind == "tool.call"]

    assert calls, f"{provider} did not report the tool call at all"
    serialised = json.dumps([event.payload for event in calls])
    for secret in ("id_rsa", "sk-secret-value"):
        assert secret not in serialised
    for call in calls:
        assert call.payload["args_redacted"] is True
