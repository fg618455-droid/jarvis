from __future__ import annotations

import json
from unittest.mock import MagicMock

from jarvis.tools.builtin import interaction_resolver


def _browser_observation() -> dict:
    controls = [
        {
            "ref": f"b7-{index}",
            "name": f"DuckDuckGo result {index} about privacy and search " + ("detail " * 12),
            "role": "link",
            "href": f"https://example{index}.test/articles/{'path/' * 20}",
            "sensitive": False,
        }
        for index in range(1, 201)
    ]
    wrappers = "\n".join("  - group:\n    - region:" for _ in range(700))
    return {
        "url": "https://duckduckgo.com/?q=privacy",
        "accessibility_tree": wrappers + "\n  - link: Privacy search result\n" + ("article text " * 2500),
        "text": "DuckDuckGo search result text " * 2400,
        "controls": controls,
    }


def _desktop_observation() -> dict:
    controls = []
    for index in range(1, 301):
        structural = index > 55
        controls.append({
            "control_id": f"c4-{index}",
            "name": "" if structural else f"Notepad command {index} " + ("formatting " * 8),
            "control_type": "Pane" if structural else ("Button" if index % 2 else "MenuItem"),
            "automation_id": "" if structural else f"Command{index}",
            "sensitive": False,
        })
    return {
        "window": {"window_id": "w3-1", "title": "Notes - Notepad"},
        "tabs": [
            {"control_id": f"c4-{index}", "name": f"Document {index}", "active": index == 1}
            for index in range(1, 21)
        ],
        "controls": controls,
    }


def test_browser_resolver_payload_stays_bounded_with_duckduckgo_scale_fixture(
    mock_config, monkeypatch,
) -> None:
    call = MagicMock(return_value='{"kind":"done","args":{"summary":"Found it."},"risk":"read_only"}')
    monkeypatch.setattr(interaction_resolver, "call_llm_direct", call)
    observation = _browser_observation()
    assert len(json.dumps(observation)) > 100_000
    assert len(observation["controls"]) == 200
    history = [
        {"kind": "browser_click", "args": {"ref": f"b{turn}-1"}, "observation": observation}
        for turn in range(1, 9)
    ]

    result = interaction_resolver.resolve_semantic_action(
        mock_config,
        surface="browser",
        task="Find the DuckDuckGo privacy result",
        observation=observation,
        history=history,
        action_contract="- browser_click {ref: current ref}\n- done {summary: text}",
    )

    payload = json.loads(call.call_args.kwargs["user_content"])
    assert result is not None
    assert len(call.call_args.kwargs["user_content"]) <= 18_000
    assert len(payload["observation"]["controls"]) <= 32
    assert all("observation" not in entry for entry in payload["history"])
    assert "  - group:" not in payload["observation"]["accessibility_tree"]


def test_desktop_resolver_payload_stays_bounded_with_complex_notepad_fixture(
    mock_config, monkeypatch,
) -> None:
    call = MagicMock(return_value='{"kind":"done","args":{"summary":"Inspected."},"risk":"read_only"}')
    monkeypatch.setattr(interaction_resolver, "call_llm_direct", call)

    observation = _desktop_observation()
    assert len(observation["controls"]) == 300
    assert len(observation["tabs"]) == 20
    assert len(json.dumps(observation)) > 30_000
    result = interaction_resolver.resolve_semantic_action(
        mock_config,
        surface="desktop",
        task="Inspect Document 12 in Notepad",
        observation=observation,
        history=[],
        action_contract="- desktop_select {control_id: current ref}\n- done {summary: text}",
    )

    payload = json.loads(call.call_args.kwargs["user_content"])
    assert result is not None
    assert len(call.call_args.kwargs["user_content"]) <= 18_000
    assert payload["observation"]["tabs"]
    assert all(item["name"] for item in payload["observation"]["controls"])


def test_resolver_keeps_done_grounding_feedback_in_bounded_observations(
    mock_config, monkeypatch,
) -> None:
    call = MagicMock(return_value='{"kind":"browser_read","args":{},"risk":"read_only"}')
    monkeypatch.setattr(interaction_resolver, "call_llm_direct", call)
    observation = _browser_observation()
    observation["resolver_feedback"] = "Read the actual UI state before answering."

    result = interaction_resolver.resolve_semantic_action(
        mock_config,
        surface="browser",
        task="Read the result",
        observation=observation,
        history=[],
        action_contract="- browser_read {}\n- done {summary?: text}",
    )

    payload = json.loads(call.call_args.kwargs["user_content"])
    assert result is not None
    assert payload["observation"]["resolver_feedback"] == observation["resolver_feedback"]


def test_resolver_retries_once_with_json_correction(mock_config, monkeypatch) -> None:
    call = MagicMock(side_effect=[
        '{"kind":"browser_click","args":{"ref":"b1-1","selector":"#result"},"risk":"ordinary"}',
        '{"kind":"done","args":{"summary":"Finished."},"risk":"read_only"}',
    ])
    monkeypatch.setattr(interaction_resolver, "call_llm_direct", call)

    result = interaction_resolver.resolve_semantic_action(
        mock_config,
        surface="browser",
        task="Finish",
        observation={"state": "ready"},
        history=[],
        action_contract="- browser_click {ref: current ref}\n- done {summary: text}",
        action_validator=lambda action: (
            None if "selector" in action["args"] else action
        ),
    )

    assert result == {"kind": "done", "args": {"summary": "Finished."}, "risk": "read_only"}
    assert call.call_count == 2
    assert "last output was not valid JSON" in call.call_args.kwargs["user_content"]


def test_resolver_logs_repair_and_gives_up_after_the_second_invalid_output(
    mock_config, monkeypatch,
) -> None:
    call = MagicMock(return_value="not json")
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(interaction_resolver, "call_llm_direct", call)
    monkeypatch.setattr(
        interaction_resolver,
        "debug_log",
        lambda message, category: logs.append((message, category)),
    )

    result = interaction_resolver.resolve_semantic_action(
        mock_config,
        surface="desktop",
        task="Finish",
        observation={"state": "ready"},
        history=[],
        action_contract="- done {summary: text}",
    )

    assert result is None
    assert call.call_count == 2
    assert ("desktop resolver retrying invalid output", "tools") in logs
    assert ("desktop resolver gave up after repair retry", "tools") in logs
