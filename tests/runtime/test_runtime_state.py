"""Behaviour tests for the live runtime state and the event bus.

The state is what makes "it ignored me" answerable: which phase it is in,
how long it has been there, and how many utterances were thrown away and
why. The bus carries those changes to whoever is watching, and must never
be able to hold a reply up.
"""

import threading
import time

import pytest

from jarvis.runtime.events import SUBSCRIBER_QUEUE_SIZE, EventBus
from jarvis.runtime.state import Phase, RuntimeState, get_runtime_state, set_phase


@pytest.fixture
def state():
    return RuntimeState()


@pytest.fixture(autouse=True)
def _clean_global_state():
    get_runtime_state().reset()
    yield
    get_runtime_state().reset()


class TestPhase:
    def test_a_fresh_state_is_starting_up(self, state):
        assert state.snapshot()["phase"] == "starting"

    def test_moving_phase_is_visible_immediately(self, state):
        state.set_phase(Phase.THINKING)

        assert state.snapshot()["phase"] == "thinking"

    def test_the_phase_carries_how_long_it_has_lasted(self, state):
        state.set_phase(Phase.IDLE)
        time.sleep(0.02)

        assert state.snapshot()["phase_seconds"] >= 0.02

    def test_re_entering_the_same_phase_does_not_restart_its_clock(self, state):
        state.set_phase(Phase.IDLE)
        time.sleep(0.02)
        state.set_phase(Phase.IDLE)

        assert state.snapshot()["phase_seconds"] >= 0.02

    def test_the_shorthand_writes_to_the_shared_state(self):
        set_phase(Phase.SPEAKING)

        assert get_runtime_state().snapshot()["phase"] == "speaking"


class TestTallies:
    def test_discarded_utterances_are_counted_by_reason(self, state):
        state.count_discard("vad")
        state.count_discard("language_probability")
        state.count_discard("vad")

        assert state.snapshot()["discarded"] == {"vad": 2, "language_probability": 1}

    def test_an_error_is_counted_and_the_latest_one_kept(self, state):
        state.record_error("first")
        state.record_error("second")

        snapshot = state.snapshot()
        assert snapshot["errors"] == 2
        assert snapshot["last_error"] == "second"

    def test_uptime_grows_from_the_start(self, state):
        time.sleep(0.02)

        assert state.snapshot()["uptime_seconds"] >= 0.02

    def test_a_reset_clears_a_session(self, state):
        state.count_discard("vad")
        state.record_error("boom")
        state.set_phase(Phase.SPEAKING)

        state.reset()

        snapshot = state.snapshot()
        assert snapshot["discarded"] == {}
        assert snapshot["errors"] == 0
        assert snapshot["last_error"] is None
        assert snapshot["phase"] == "starting"


class TestDescription:
    def test_models_and_audio_are_reported_as_given(self, state):
        state.describe_models(chat="qwen2.5:7b", embedding="nomic-embed-text")
        state.describe_audio(microphone="ZTD39")

        snapshot = state.snapshot()
        assert snapshot["models"]["chat"] == "qwen2.5:7b"
        assert snapshot["audio"]["microphone"] == "ZTD39"

    def test_an_unknown_value_does_not_overwrite_a_known_one(self, state):
        state.describe_models(chat="qwen2.5:7b")
        state.describe_models(chat=None, fast="qwen2.5:7b")

        assert state.snapshot()["models"]["chat"] == "qwen2.5:7b"


class TestEventBus:
    def test_a_subscriber_receives_what_is_published(self):
        bus = EventBus()

        with bus.subscribe() as subscription:
            bus.publish("phase", {"phase": "thinking"})
            event = next(subscription.listen(timeout=1.0))

        assert event == {"kind": "phase", "data": {"phase": "thinking"}}

    def test_every_subscriber_receives_the_same_event(self):
        bus = EventBus()

        with bus.subscribe() as one, bus.subscribe() as two:
            bus.publish("turn", {"turn_id": "abc"})

            assert next(one.listen(timeout=1.0))["data"]["turn_id"] == "abc"
            assert next(two.listen(timeout=1.0))["data"]["turn_id"] == "abc"

    def test_a_lapsed_wait_yields_nothing_rather_than_hanging(self):
        bus = EventBus()

        with bus.subscribe() as subscription:
            assert next(subscription.listen(timeout=0.01)) is None

    def test_publishing_with_nobody_watching_is_harmless(self):
        EventBus().publish("phase", {"phase": "idle"})

    def test_a_stalled_watcher_never_blocks_the_publisher(self):
        """The reply path publishes; a page nobody is reading must not stall it."""
        bus = EventBus()
        subscription = bus.subscribe()

        begun = time.perf_counter()
        for index in range(SUBSCRIBER_QUEUE_SIZE * 3):
            bus.publish("stage", {"n": index})
        elapsed = time.perf_counter() - begun

        assert elapsed < 1.0
        assert next(subscription.listen(timeout=1.0))["data"]["n"] > 0

    def test_a_closed_subscriber_stops_receiving(self):
        bus = EventBus()
        subscription = bus.subscribe()
        subscription.close()

        bus.publish("phase", {"phase": "idle"})

        assert bus.subscriber_count == 0

    def test_closing_twice_is_harmless(self):
        bus = EventBus()
        subscription = bus.subscribe()
        subscription.close()
        subscription.close()

        assert bus.subscriber_count == 0

    def test_subscribers_can_come_and_go_while_events_flow(self):
        bus = EventBus()
        stop = threading.Event()

        def publisher():
            while not stop.is_set():
                bus.publish("stage", {})

        thread = threading.Thread(target=publisher, daemon=True)
        thread.start()
        try:
            for _ in range(50):
                bus.subscribe().close()
        finally:
            stop.set()
            thread.join(timeout=2.0)

        assert bus.subscriber_count == 0
