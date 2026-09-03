"""Behaviour tests for the face's live-state API.

The face polls /api/visualizer/state for its reading: idle, listening,
thinking or speaking, derived from Jarvis's own runtime phase and from what
a TTS engine last wrote to the speakers. These tests exercise the real Flask
route, because the phase-to-state mapping and the blueprint wiring are both
things a direct function call would skip.
"""

import pytest

from jarvis.runtime import get_runtime_state
from jarvis.runtime.state import Phase
from jarvis.webui.server import WebUIConfig, create_app
from jarvis.webui.visualizer.state import get_visualizer_waveform


HEADERS = {"Host": "127.0.0.1:5055"}


@pytest.fixture
def client():
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(autouse=True)
def _clean_runtime():
    get_runtime_state().reset()
    get_visualizer_waveform().reset()
    yield
    get_runtime_state().reset()
    get_visualizer_waveform().reset()


class TestVisualizerState:
    @pytest.mark.parametrize(
        "phase, expected",
        [
            (Phase.IDLE, "idle"),
            (Phase.STARTING, "idle"),
            (Phase.DICTATING, "idle"),
            (Phase.CAPTURING, "listening"),
            (Phase.TRANSCRIBING, "thinking"),
            (Phase.THINKING, "thinking"),
            (Phase.TOOL, "thinking"),
            (Phase.SPEAKING, "speaking"),
        ],
    )
    def test_state_follows_the_runtime_phase(self, client, phase, expected):
        get_runtime_state().set_phase(phase)

        body = client.get("/api/visualizer/state", headers=HEADERS).get_json()

        assert body["state"] == expected

    def test_state_carries_the_shape_the_face_expects(self, client):
        body = client.get("/api/visualizer/state", headers=HEADERS).get_json()

        assert set(body) == {"state", "level", "samples", "alert", "loading"}
        assert isinstance(body["samples"], list)
        assert len(body["samples"]) == 64
        assert body["alert"] is False
        assert body["loading"] is False

    def test_a_fresh_waveform_overrides_a_stale_phase_reading(self, client):
        """A phase that has not caught up yet loses to real audio: what is
        actually leaving the speakers is the better reading of the two."""
        get_runtime_state().set_phase(Phase.THINKING)
        get_visualizer_waveform().feed([3000] * 64)

        body = client.get("/api/visualizer/state", headers=HEADERS).get_json()

        assert body["state"] == "speaking"
        assert body["level"] > 0
        assert any(sample != 0 for sample in body["samples"])

    def test_a_stale_waveform_is_ignored(self, client, monkeypatch):
        import time as time_module

        get_runtime_state().set_phase(Phase.IDLE)
        waveform = get_visualizer_waveform()
        waveform.feed([3000] * 64)
        # Push the recorded timestamp far enough into the past that it
        # reads as stale on the next poll.
        waveform._ts -= 10  # noqa: SLF001 - test reaches into the holder deliberately

        body = client.get("/api/visualizer/state", headers=HEADERS).get_json()

        assert body["state"] == "idle"
        assert body["level"] == 0.0
        assert all(sample == 0 for sample in body["samples"])
