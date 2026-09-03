"""Behaviour tests for a phase that nobody hands back.

Every mid-turn phase is handed back by the stage that follows it, and speech
normally closes a turn. But the handback is conditional: it only fires while
the phase is still the one that stage claimed. When another stage has moved
on in the meantime, the handback is skipped by design, and a phase can be
left standing after the turn it belonged to is long finished.

That is invisible to the assistant and highly visible to the user: the UI
keeps saying "running a tool" for minutes while the answer was spoken ages
ago, which reads as a hang. The state must therefore heal itself.
"""

import time

import pytest

from jarvis.runtime.state import Phase, RuntimeState


class TestSpeechEndsEveryMidTurnPhase:
    def test_tool_phase_is_released_when_speech_ends(self):
        """A tool still marked as running when speech ends must not persist."""
        state = RuntimeState()
        state.set_phase(Phase.THINKING)
        state.set_phase_if(Phase.THINKING, Phase.TOOL)

        state.end_turn_phase()

        assert state.phase is Phase.IDLE

    @pytest.mark.parametrize("phase", [Phase.THINKING, Phase.TOOL, Phase.SPEAKING])
    def test_every_mid_turn_phase_returns_to_idle(self, phase):
        state = RuntimeState()
        state.set_phase(phase)

        state.end_turn_phase()

        assert state.phase is Phase.IDLE

    def test_dictation_is_not_a_turn_and_survives(self):
        """Hold-to-dictate owns the microphone independently of any turn."""
        state = RuntimeState()
        state.set_phase(Phase.DICTATING)

        state.end_turn_phase()

        assert state.phase is Phase.DICTATING


class TestStalePhaseWatchdog:
    def test_a_phase_left_standing_heals_itself(self):
        state = RuntimeState(phase_watchdog_sec=0.05)
        state.set_phase(Phase.TOOL)
        time.sleep(0.08)

        assert state.snapshot()["phase"] == Phase.IDLE.value

    def test_a_working_phase_is_left_alone(self):
        """A tool that genuinely runs must keep its phase."""
        state = RuntimeState(phase_watchdog_sec=30.0)
        state.set_phase(Phase.TOOL)

        assert state.snapshot()["phase"] == Phase.TOOL.value

    def test_dictation_never_expires(self):
        state = RuntimeState(phase_watchdog_sec=0.05)
        state.set_phase(Phase.DICTATING)
        time.sleep(0.08)

        assert state.snapshot()["phase"] == Phase.DICTATING.value

    def test_idle_does_not_thrash_phase_since(self):
        """Healing must be a no-op once the state is already idle."""
        state = RuntimeState(phase_watchdog_sec=0.05)
        state.set_phase(Phase.IDLE)
        since = state.phase_since
        time.sleep(0.08)
        state.snapshot()

        assert state.phase_since == since
