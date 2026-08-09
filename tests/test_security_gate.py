from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jarvis.config import load_settings
from jarvis.security.desktop_confirm import DesktopConfirm
from jarvis.security.gate import SecurityGate
from jarvis.security.telegram_confirm import TelegramConfirm
from jarvis.security.voice_confirm import VoiceConsoleConfirm
from jarvis.tools.base import Tool
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult


class RecordingTool(Tool):
    def __init__(self) -> None:
        self.executed = False

    @property
    def name(self) -> str:
        return "recordingTool"

    @property
    def description(self) -> str:
        return "Records whether execution was reached."

    @property
    def inputSchema(self) -> dict:
        return {"type": "object", "properties": {}}

    def run(self, args, context) -> ToolExecutionResult:
        self.executed = True
        return ToolExecutionResult(success=True, reply_text="executed")


class DecisionChannel:
    def __init__(self, decision: bool, *, available: bool = True) -> None:
        self.decision = decision
        self.is_available = available
        self.requests: list[tuple[str, dict]] = []

    def ask(self, action_name: str, action_args: dict) -> bool:
        self.requests.append((action_name, action_args))
        return self.decision


class FailingChannel:
    is_available = True

    def ask(self, action_name: str, action_args: dict) -> bool:
        raise RuntimeError("channel disconnected")


def _run(cfg, name: str, args: dict | None = None) -> ToolExecutionResult:
    return run_tool_with_retries(
        db=None,
        cfg=cfg,
        tool_name=name,
        tool_args=args or {},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )


@pytest.fixture(autouse=True)
def _reset_gate() -> None:
    SecurityGate.reset_instance()
    yield
    SecurityGate.reset_instance()


def test_critical_level_denies_a_builtin_mutation_before_execution(mock_config) -> None:
    tool = RecordingTool()
    original = BUILTIN_TOOLS.get("deleteMeal")
    BUILTIN_TOOLS["deleteMeal"] = tool
    rejecting = DecisionChannel(False)
    SecurityGate(level="critical", channels={"desktop": rejecting}, confirm_channels=["desktop"])
    cfg = replace(mock_config, security_level="critical")

    try:
        result = _run(cfg, "deleteMeal", {"id": 7})
    finally:
        if original is not None:
            BUILTIN_TOOLS["deleteMeal"] = original

    assert result.success is False
    assert "denied" in (result.error_message or "").lower()
    assert tool.executed is False


def test_critical_level_allows_read_only_builtin_without_confirmation(mock_config) -> None:
    tool = RecordingTool()
    original = BUILTIN_TOOLS.get("fetchMeals")
    BUILTIN_TOOLS["fetchMeals"] = tool
    rejecting = DecisionChannel(False)
    SecurityGate(level="critical", channels={"desktop": rejecting}, confirm_channels=["desktop"])
    cfg = replace(mock_config, security_level="critical")

    try:
        result = _run(cfg, "fetchMeals")
    finally:
        if original is not None:
            BUILTIN_TOOLS["fetchMeals"] = original

    assert result.success is True
    assert tool.executed is True
    assert rejecting.requests == []


@pytest.mark.parametrize("operation", ["write", "append", "delete"])
def test_critical_level_gates_local_file_mutations(mock_config, operation: str) -> None:
    rejecting = DecisionChannel(False)
    gate = SecurityGate(level="critical", channels={"desktop": rejecting}, confirm_channels=["desktop"])

    assert gate.confirm("localFiles", {"operation": operation, "path": "note.txt"}) is False


@pytest.mark.parametrize("operation", [" write", "WRITE", "\tDelete "])
def test_local_file_mutations_are_gated_exactly_as_the_tool_reads_them(operation: str) -> None:
    rejecting = DecisionChannel(False)
    gate = SecurityGate(level="critical", channels={"desktop": rejecting}, confirm_channels=["desktop"])

    assert gate.confirm("localFiles", {"operation": operation, "path": "note.txt"}) is False


@pytest.mark.parametrize("operation", ["list", "read"])
def test_critical_level_does_not_gate_local_file_reads(operation: str) -> None:
    rejecting = DecisionChannel(False)
    gate = SecurityGate(level="critical", channels={"desktop": rejecting}, confirm_channels=["desktop"])

    assert gate.confirm("localFiles", {"operation": operation, "path": "note.txt"}) is True
    assert rejecting.requests == []


def test_paranoid_level_gates_every_valid_tool(mock_config) -> None:
    tool = RecordingTool()
    original = BUILTIN_TOOLS.get("getWeather")
    BUILTIN_TOOLS["getWeather"] = tool
    rejecting = DecisionChannel(False)
    SecurityGate(level="paranoid", channels={"desktop": rejecting}, confirm_channels=["desktop"])
    cfg = replace(mock_config, security_level="paranoid")

    try:
        result = _run(cfg, "getWeather")
    finally:
        if original is not None:
            BUILTIN_TOOLS["getWeather"] = original

    assert result.success is False
    assert tool.executed is False


def test_off_level_allows_critical_tool_without_confirmation() -> None:
    rejecting = DecisionChannel(False)
    gate = SecurityGate(level="off", channels={"desktop": rejecting}, confirm_channels=["desktop"])

    assert gate.confirm("deleteMeal", {"id": 7}) is True
    assert rejecting.requests == []


def test_all_mcp_tools_are_critical_and_fail_closed(mock_config) -> None:
    SecurityGate(level="critical", channels={}, confirm_channels=["desktop", "telegram", "voice"])
    cfg = replace(
        mock_config,
        security_level="critical",
        mcps={"mail": {"command": "unused"}},
    )

    result = _run(cfg, "mail__send_message", {"recipient": "person@example.test"})

    assert result.success is False
    assert "denied" in (result.error_message or "").lower()


def test_unavailable_or_failed_channel_can_fall_through_to_next_channel() -> None:
    approving = DecisionChannel(True)
    gate = SecurityGate(
        level="critical",
        channels={
            "unavailable": DecisionChannel(False, available=False),
            "failed": FailingChannel(),
            "voice": approving,
        },
        confirm_channels=["unavailable", "failed", "voice"],
    )

    assert gate.confirm("deleteMeal", {"id": 7}) is True
    assert approving.requests == [("deleteMeal", {"id": 7})]


def test_explicit_denial_does_not_fall_through_to_another_channel() -> None:
    rejecting = DecisionChannel(False)
    approving = DecisionChannel(True)
    gate = SecurityGate(
        level="critical",
        channels={"desktop": rejecting, "voice": approving},
        confirm_channels=["desktop", "voice"],
    )

    assert gate.confirm("deleteMeal", {"id": 7}) is False
    assert approving.requests == []


def test_the_live_gate_follows_the_current_settings(mock_config) -> None:
    """The bundled desktop app restarts the daemon thread, not the process."""
    tool = RecordingTool()
    original = BUILTIN_TOOLS.get("getWeather")
    BUILTIN_TOOLS["getWeather"] = tool
    try:
        relaxed = replace(mock_config, security_level="off")
        assert _run(relaxed, "getWeather").success is True

        tool.executed = False
        tightened = replace(
            mock_config,
            security_level="paranoid",
            security_confirm_channels=[],
        )
        result = _run(tightened, "getWeather")
    finally:
        if original is not None:
            BUILTIN_TOOLS["getWeather"] = original

    assert result.success is False
    assert tool.executed is False


def test_real_config_file_wires_every_security_setting(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "security_level": "paranoid",
        "security_confirm_channels": ["voice", "telegram"],
        "security_confirmation_timeout_sec": 23,
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "123456",
    }), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    cfg = load_settings()

    assert cfg.security_level == "paranoid"
    assert cfg.security_confirm_channels == ["voice", "telegram"]
    assert cfg.security_confirmation_timeout_sec == 23
    assert cfg.telegram_bot_token == "test-token"
    assert cfg.telegram_chat_id == "123456"

    rejecting = DecisionChannel(False)
    gate = SecurityGate.from_settings(cfg, channels={"voice": rejecting})
    assert gate.confirm("getWeather", {}) is False
    assert rejecting.requests == [("getWeather", {})]


def test_every_security_config_key_is_exposed_in_settings_metadata() -> None:
    from desktop_app.settings_window import FIELD_METADATA

    exposed = {field.key for field in FIELD_METADATA}
    assert {
        "security_level",
        "security_confirm_channels",
        "security_confirmation_timeout_sec",
        "telegram_bot_token",
        "telegram_chat_id",
    } <= exposed


def test_voice_confirmation_uses_the_numeric_challenge_not_language_words() -> None:
    heard: list[str] = []

    def voice_request(action_name: str, action_args: dict, challenge: str, timeout: int) -> str:
        heard.append(challenge)
        return "٤ ٢ ٧ ٩"

    channel = VoiceConsoleConfirm(
        timeout_seconds=10,
        voice_requester=voice_request,
        challenge_factory=lambda: "4279",
    )

    assert channel.ask("desktop__click", {"x": 1}) is True
    assert heard == ["4279"]


def test_voice_confirmation_rejects_any_other_transcript() -> None:
    channel = VoiceConsoleConfirm(
        timeout_seconds=10,
        voice_requester=lambda *_args: "yes",
        challenge_factory=lambda: "4279",
    )

    assert channel.ask("desktop__click", {"x": 1}) is False


def test_console_confirmation_times_out_as_a_denial() -> None:
    blocker = threading.Event()
    channel = VoiceConsoleConfirm(
        timeout_seconds=0.05,
        input_reader=lambda _prompt: blocker.wait(5) or "4279",
        challenge_factory=lambda: "4279",
    )

    started = time.monotonic()
    assert channel.ask("desktop__click", {"x": 1}) is False
    assert time.monotonic() - started < 0.5


def test_voice_listener_speaks_then_captures_a_confirmation_transcript(mock_config) -> None:
    import numpy as np
    from jarvis.listening.listener import VoiceListener

    class FakeTTS:
        enabled = True

        def speak(self, text, completion_callback=None, duration_callback=None) -> None:
            if completion_callback:
                completion_callback()

        def is_speaking(self) -> bool:
            return False

    listener = VoiceListener.__new__(VoiceListener)
    listener.cfg = SimpleNamespace(
        voice_min_energy=0.02,
        endpoint_silence_ms=100,
        whisper_language="",
    )
    listener.tts = FakeTTS()
    listener.model = object()
    listener._whisper_backend = "faster-whisper"
    listener._audio_q = queue.Queue()
    listener._samplerate = 16_000
    listener._stop_thinking_tune = lambda: None
    listener._clear_audio_buffers = lambda: None
    listener._transcribe_security_audio = lambda audio: "4 2 7 9"

    def provide_audio() -> None:
        time.sleep(0.05)
        listener._audio_q.put(np.full((1600, 1), 0.1, dtype=np.float32))
        listener._audio_q.put(np.zeros((3200, 1), dtype=np.float32))

    threading.Thread(target=provide_audio, daemon=True).start()

    assert listener.request_security_confirmation("desktop__click", {}, "4279", 2) == "4 2 7 9"


def test_desktop_channel_returns_the_dialog_decision() -> None:
    channel = DesktopConfirm(
        timeout_seconds=10,
        requester=lambda action, args, timeout: action == "mail__send" and args["count"] == 1,
    )

    assert channel.ask("mail__send", {"count": 1}) is True


def test_qt_security_dialog_approves_only_from_the_approve_button(qapp) -> None:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QDialog
    from desktop_app.security_confirmation import SecurityConfirmationDialog

    dialog = SecurityConfirmationDialog("shell__run", {"command": "whoami"}, timeout_seconds=10)
    QTimer.singleShot(0, dialog.approve_button.click)

    assert dialog.exec() == QDialog.DialogCode.Accepted


class FakeTelegramTransport:
    def __init__(self, updates: list[dict]) -> None:
        self.updates = updates
        self.sent: list[tuple[str, dict]] = []

    def post(self, method: str, payload: dict, timeout: float) -> dict:
        self.sent.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 1}}
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return {"ok": True, "result": updates}
        if method == "editMessageText":
            return {"ok": True, "result": {}}
        raise AssertionError(method)


def test_telegram_is_unavailable_without_credentials() -> None:
    assert TelegramConfirm("", "", timeout_seconds=10).is_available is False


def test_telegram_approves_only_an_authorised_matching_callback() -> None:
    transport = FakeTelegramTransport([
        {
            "update_id": 10,
            "callback_query": {
                "id": "wrong-chat",
                "data": "approve:req-fixed",
                "message": {"chat": {"id": 999}, "message_id": 1},
            },
        },
        {
            "update_id": 11,
            "callback_query": {
                "id": "authorised",
                "data": "approve:req-fixed",
                "message": {"chat": {"id": 123456}, "message_id": 1},
            },
        },
    ])
    channel = TelegramConfirm(
        "token",
        "123456",
        timeout_seconds=10,
        transport=transport,
        request_id_factory=lambda: "req-fixed",
    )

    assert channel.ask("mail__send", {"to": "person@example.test"}) is True
    assert transport.sent[0][0] == "sendMessage"


def test_telegram_denial_is_a_final_false_decision() -> None:
    transport = FakeTelegramTransport([{
        "update_id": 12,
        "callback_query": {
            "id": "authorised",
            "data": "deny:req-fixed",
            "message": {"chat": {"id": 123456}, "message_id": 1},
        },
    }])
    channel = TelegramConfirm(
        "token",
        "123456",
        timeout_seconds=10,
        transport=transport,
        request_id_factory=lambda: "req-fixed",
    )

    assert channel.ask("mail__send", {}) is False


def test_telegram_timeout_is_testable_without_network_or_token() -> None:
    class AdvancingClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 1.0
            return self.value

    transport = FakeTelegramTransport([])
    channel = TelegramConfirm(
        "token",
        "123456",
        timeout_seconds=2,
        transport=transport,
        request_id_factory=lambda: "req-fixed",
        clock=AdvancingClock(),
    )

    assert channel.ask("mail__send", {}) is False
