"""Protocol tests for the main-process Claude subscription sidecar client."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_claude_sidecar.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _client(script: Path, **kwargs):
    from jarvis.llm.claude_subscription_sidecar_client import (
        ClaudeSubscriptionSidecarClient,
    )

    return ClaudeSubscriptionSidecarClient(
        interpreter_path=Path(sys.executable),
        entrypoint_path=script,
        **kwargs,
    )


def test_request_response_round_trip_and_streaming_chunks(tmp_path):
    script = _script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type": "ready"}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request["cmd"] == "shutdown":
                break
            print(json.dumps({"type": "chunk", "id": request["id"], "text": "pon"}), flush=True)
            print(json.dumps({"type": "chunk", "id": request["id"], "text": "g"}), flush=True)
            print(json.dumps({
                "type": "result", "id": request["id"],
                "text": request["system_prompt"] + ":" + request["prompt"],
            }), flush=True)
        """,
    )
    chunks: list[str] = []
    client = _client(script)
    try:
        result = client.generate("model", "system", "ping", 2.0, chunks.append)
    finally:
        client.stop()

    assert result == "system:ping"
    assert chunks == ["pon", "g"]


def test_sidecar_death_mid_call_is_a_sanitised_sidecar_error(tmp_path):
    from jarvis.llm.claude_subscription_sidecar_client import ClaudeSidecarError

    script = _script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type": "ready"}), flush=True)
        sys.stdin.readline()
        raise SystemExit(7)
        """,
    )
    client = _client(script)
    with pytest.raises(ClaudeSidecarError) as raised:
        client.generate("model", "system", "secret prompt", 2.0)

    assert "secret prompt" not in str(raised.value)
    assert not client.is_running


def test_a_call_after_sidecar_death_launches_a_clean_process(tmp_path):
    from jarvis.llm.claude_subscription_sidecar_client import ClaudeSidecarError

    marker = tmp_path / "first-process.marker"
    script = _script(
        tmp_path,
        f"""
        import json, pathlib, sys
        marker = pathlib.Path({str(marker)!r})
        print(json.dumps({{"type": "ready"}}), flush=True)
        request = json.loads(sys.stdin.readline())
        if not marker.exists():
            marker.write_text("used")
            raise SystemExit(7)
        print(json.dumps({{"type": "result", "id": request["id"], "text": "recovered"}}), flush=True)
        """,
    )
    client = _client(script)
    try:
        with pytest.raises(ClaudeSidecarError):
            client.generate("model", "system", "first", 2.0)
        assert client.generate("model", "system", "second", 2.0) == "recovered"
    finally:
        client.stop()


def test_sidecar_that_never_reports_ready_times_out(tmp_path):
    from jarvis.llm.claude_subscription_sidecar_client import ClaudeSidecarError

    script = _script(
        tmp_path,
        """
        import time
        time.sleep(30)
        """,
    )
    client = _client(script, ready_timeout_sec=0.05)
    with pytest.raises(ClaudeSidecarError, match="did not become ready"):
        client.generate("model", "system", "prompt", 2.0)
    assert not client.is_running


def test_missing_sidecar_environment_is_typed_and_does_not_spawn(tmp_path):
    from jarvis.llm.claude_subscription_sidecar_client import (
        ClaudeSidecarError,
        ClaudeSubscriptionSidecarClient,
    )

    missing = tmp_path / "missing" / "python.exe"
    client = ClaudeSubscriptionSidecarClient(interpreter_path=missing)
    with patch("jarvis.llm.claude_subscription_sidecar_client.subprocess.Popen") as popen:
        with pytest.raises(ClaudeSidecarError, match="environment is not installed"):
            client.generate("model", "system", "prompt", 2.0)
    popen.assert_not_called()


def test_error_status_is_preserved_without_exposing_sidecar_details(tmp_path):
    from jarvis.llm.claude_subscription_sidecar_client import ClaudeSidecarError

    script = _script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type": "ready"}), flush=True)
        request = json.loads(sys.stdin.readline())
        print(json.dumps({
            "type": "error", "id": request["id"], "status": 401,
            "message": "credential and path that must not escape",
        }), flush=True)
        """,
    )
    client = _client(script)
    with pytest.raises(ClaudeSidecarError) as raised:
        client.generate("model", "system", "prompt", 2.0)

    assert raised.value.status == 401
    assert "credential" not in str(raised.value)


def test_tool_denial_event_is_recorded_without_tool_input(tmp_path):
    script = _script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type": "ready"}), flush=True)
        request = json.loads(sys.stdin.readline())
        print(json.dumps({
            "type": "tool_denied", "id": request["id"], "tool_name": "account_connector",
        }), flush=True)
        print(json.dumps({"type": "result", "id": request["id"], "text": "safe"}), flush=True)
        """,
    )
    client = _client(script)
    with patch("jarvis.llm.claude_subscription_sidecar_client.debug_log") as logged:
        assert client.generate("model", "system", "private input", 2.0) == "safe"

    messages = [str(call.args[0]) for call in logged.call_args_list]
    assert any("denied" in message and "account_connector" in message for message in messages)
    assert all("private input" not in message for message in messages)


def test_provider_error_does_not_stop_the_sidecar(tmp_path):
    from jarvis.llm.claude_subscription_sidecar_client import ClaudeSidecarError

    script = _script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type": "ready"}), flush=True)
        calls = 0
        for line in sys.stdin:
            request = json.loads(line)
            if request["cmd"] == "shutdown":
                break
            calls += 1
            if calls == 1:
                print(json.dumps({"type": "error", "id": request["id"], "status": 429}), flush=True)
            else:
                print(json.dumps({"type": "result", "id": request["id"], "text": "recovered"}), flush=True)
        """,
    )
    client = _client(script)
    try:
        with pytest.raises(ClaudeSidecarError) as raised:
            client.generate("model", "system", "first", 2.0)
        assert raised.value.status == 429
        assert client.generate("model", "system", "second", 2.0) == "recovered"
    finally:
        client.stop()


def test_anthropic_api_key_is_not_inherited_by_the_sidecar(tmp_path, monkeypatch):
    script = _script(
        tmp_path,
        """
        import json, os, sys
        print(json.dumps({"type": "ready"}), flush=True)
        request = json.loads(sys.stdin.readline())
        state = "present" if os.environ.get("ANTHROPIC_API_KEY") else "absent"
        print(json.dumps({"type": "result", "id": request["id"], "text": state}), flush=True)
        """,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-the-boundary")
    client = _client(script)
    try:
        assert client.generate("model", "system", "prompt", 2.0) == "absent"
    finally:
        client.stop()


def test_protocol_preserves_unicode_text_on_windows(tmp_path):
    script = _script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type": "ready"}, ensure_ascii=False), flush=True)
        request = json.loads(sys.stdin.readline())
        print(json.dumps({
            "type": "result", "id": request["id"], "text": "Grüße — 世界",
        }, ensure_ascii=False), flush=True)
        """,
    )
    client = _client(script)
    try:
        assert client.generate("model", "system", "prompt", 2.0) == "Grüße — 世界"
    finally:
        client.stop()


def test_default_ready_timeout_tolerates_a_slow_cold_start():
    """A fresh sidecar interpreter importing the Claude Agent SDK and
    authenticating a session has been observed taking several seconds
    (~6-11s) to report ready. The default must clear that with headroom, or
    every merely-slow-not-broken start gets misreported as a provider
    failure and silently falls back to a different chat backend."""
    from jarvis.llm.claude_subscription_sidecar_client import (
        ClaudeSubscriptionSidecarClient,
    )

    client = ClaudeSubscriptionSidecarClient()
    assert client._ready_timeout_sec >= 15.0
