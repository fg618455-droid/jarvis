"""Confirm a tool once, then never again for that tool.

Answering the same dialog for every calendar entry is friction that teaches
the user to approve without reading, which costs more safety than it buys.
The remembered approval trades a real boundary for that: an approval is
recorded per action name, so a later call of the same tool with entirely
different arguments no longer asks. It is off by default for exactly that
reason, and only an approval is ever remembered.
"""

from __future__ import annotations

import json

import pytest

from jarvis.security.approvals import ApprovalStore
from jarvis.security.gate import SecurityGate


class _Channel:
    """A channel that answers as instructed and counts how often it was asked."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    @property
    def is_available(self) -> bool:
        return True

    def ask(self, action_name, action_args) -> bool:
        self.asked.append(action_name)
        return self.answer


def _gate(tmp_path, channel, *, remember=True, level="critical") -> SecurityGate:
    return SecurityGate(
        level=level,
        channels={"desktop": channel},
        confirm_channels=["desktop"],
        approvals=ApprovalStore(tmp_path / "approvals.json") if remember else None,
    )


class TestOneApprovalCoversLaterCalls:
    def test_the_same_tool_is_asked_about_only_once(self, tmp_path):
        channel = _Channel(answer=True)
        gate = _gate(tmp_path, channel)

        assert gate.confirm("composio__CREATE_EVENT", {"title": "Zahnarzt"}) is True
        assert gate.confirm("composio__CREATE_EVENT", {"title": "Training"}) is True

        assert channel.asked == ["composio__CREATE_EVENT"]

    def test_a_different_tool_still_asks(self, tmp_path):
        channel = _Channel(answer=True)
        gate = _gate(tmp_path, channel)

        gate.confirm("composio__CREATE_EVENT", {})
        gate.confirm("composio__SEND_EMAIL", {})

        assert channel.asked == ["composio__CREATE_EVENT", "composio__SEND_EMAIL"]

    def test_the_approval_survives_a_restart(self, tmp_path):
        first = _Channel(answer=True)
        _gate(tmp_path, first).confirm("askCrew", {"agent": "schule", "task": "x"})

        second = _Channel(answer=True)
        gate = _gate(tmp_path, second)
        assert gate.confirm("askCrew", {"agent": "dev", "task": "y"}) is True

        assert second.asked == []

    def test_an_inner_action_is_remembered_on_its_own(self, tmp_path):
        """browserInteract's inner actions are separate action names, so
        approving the tool does not silently approve every click inside it."""
        channel = _Channel(answer=True)
        gate = _gate(tmp_path, channel)

        gate.confirm("browserInteract", {})
        gate.confirm("browserInteract.click", {"control": "Buy"})

        assert channel.asked == ["browserInteract", "browserInteract.click"]


class TestARefusalIsNeverRemembered:
    def test_a_refused_tool_is_asked_about_again(self, tmp_path):
        """Remembering a no would silently block the tool for good, with
        nothing on screen to explain why it stopped working."""
        refusing = _Channel(answer=False)
        gate = _gate(tmp_path, refusing)

        assert gate.confirm("composio__DELETE_EVENT", {}) is False
        assert gate.confirm("composio__DELETE_EVENT", {}) is False

        assert refusing.asked == ["composio__DELETE_EVENT", "composio__DELETE_EVENT"]

    def test_a_refusal_does_not_reach_the_file(self, tmp_path):
        path = tmp_path / "approvals.json"
        SecurityGate(
            level="critical",
            channels={"desktop": _Channel(answer=False)},
            confirm_channels=["desktop"],
            approvals=ApprovalStore(path),
        ).confirm("composio__DELETE_EVENT", {})

        if path.exists():
            assert "composio__DELETE_EVENT" not in path.read_text(encoding="utf-8")

    def test_no_available_channel_is_not_remembered(self, tmp_path):
        """A denial for want of anyone to ask is not the user's decision."""
        path = tmp_path / "approvals.json"
        gate = SecurityGate(
            level="critical", channels={}, confirm_channels=["desktop"],
            approvals=ApprovalStore(path),
        )

        assert gate.confirm("askCrew", {}) is False
        assert not path.exists()


class TestItStaysOffUnlessAskedFor:
    def test_without_a_store_every_call_asks(self, tmp_path):
        channel = _Channel(answer=True)
        gate = _gate(tmp_path, channel, remember=False)

        gate.confirm("composio__CREATE_EVENT", {})
        gate.confirm("composio__CREATE_EVENT", {})

        assert len(channel.asked) == 2

    def test_forgetting_brings_the_question_back(self, tmp_path):
        channel = _Channel(answer=True)
        store = ApprovalStore(tmp_path / "approvals.json")
        gate = SecurityGate(level="critical", channels={"desktop": channel},
                            confirm_channels=["desktop"], approvals=store)

        gate.confirm("askCrew", {})
        store.forget_all()
        gate.confirm("askCrew", {})

        assert len(channel.asked) == 2

    def test_a_tool_that_never_needed_confirming_is_not_recorded(self, tmp_path):
        """Only a real decision is worth storing; a routine read has none."""
        path = tmp_path / "approvals.json"
        gate = SecurityGate(level="critical", channels={"desktop": _Channel()},
                            confirm_channels=["desktop"], approvals=ApprovalStore(path))

        assert gate.confirm("getTime", {}) is True
        assert not path.exists()


class TestTheStoreSurvivesABadFile:
    @pytest.mark.parametrize("written", ["", "not json", "[]", '{"approved": "x"}'])
    def test_an_unreadable_file_asks_rather_than_permits(self, tmp_path, written):
        """Fail closed: a file we cannot read must never be taken as
        blanket approval."""
        path = tmp_path / "approvals.json"
        path.write_text(written, encoding="utf-8")

        store = ApprovalStore(path)

        assert store.is_approved("composio__CREATE_EVENT") is False

    def test_a_recorded_approval_is_written_as_a_plain_name_list(self, tmp_path):
        path = tmp_path / "approvals.json"
        store = ApprovalStore(path)

        store.remember("composio__CREATE_EVENT")

        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["approved"] == ["composio__CREATE_EVENT"]


class TestTheFlagIsWiredFromARealConfigFile:
    def test_a_configured_flag_gives_the_gate_a_store(self, tmp_path, monkeypatch):
        from jarvis.config import load_settings

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"security_remember_approvals": True}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        cfg = load_settings()
        assert cfg.security_remember_approvals is True

        SecurityGate.reset_instance()
        gate = SecurityGate.from_settings(cfg, channels={"desktop": _Channel()})
        assert gate.approvals is not None

    def test_it_defaults_off(self, tmp_path, monkeypatch):
        from jarvis.config import load_settings

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        cfg = load_settings()
        assert cfg.security_remember_approvals is False

        SecurityGate.reset_instance()
        gate = SecurityGate.from_settings(cfg, channels={"desktop": _Channel()})
        assert gate.approvals is None

    def test_turning_it_off_replaces_the_running_gate(self, tmp_path, monkeypatch):
        """The flag has to reach a gate that is already up, or the change
        would sit in the file doing nothing until the next restart."""
        from jarvis.config import load_settings

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"security_remember_approvals": True}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        on = load_settings()

        SecurityGate.reset_instance()
        SecurityGate.from_settings(on, channels={"desktop": _Channel()})

        cfg_path.write_text(json.dumps({"security_remember_approvals": False}))
        off = load_settings()

        assert SecurityGate.get_or_create(off).approvals is None
