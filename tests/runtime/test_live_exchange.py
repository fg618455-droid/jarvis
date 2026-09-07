"""The exchange while it is still happening.

A finished turn carries the question, the answer and every timing at once,
which is enough to build a table of turns that are over and not enough to
build a conversation. Two more things are published as they become true:
what was understood, the moment a turn is accepted and before any answer
exists, and the answer itself, in the pieces it is written in.

Neither is a measurement, so neither may cost the turn anything. Publishing
from outside a turn is silence rather than an error, because the call sites
are ordinary reply-path code that can also be reached without one.
"""

from __future__ import annotations

import pytest

from jarvis.runtime import get_event_bus, get_recorder, publish_heard, publish_reply
from jarvis.runtime.events import Subscription


def drain(subscription: Subscription, timeout: float = 1.0) -> list[dict]:
    """Every event already waiting, and nothing else."""
    events = []
    for event in subscription.listen(timeout=timeout):
        if event is None:
            break
        events.append(event)
    return events


@pytest.fixture(autouse=True)
def _no_turn_left_behind():
    """A turn is held per thread, so one left open leaks into the next test."""
    get_recorder().abandon()
    yield
    get_recorder().abandon()


class TestWhatWasUnderstood:
    def test_it_reaches_a_watcher_before_any_answer_exists(self):
        trace = get_recorder().begin(source="voice")
        trace.transcript = "wie ist das wetter"

        with get_event_bus().subscribe() as subscription:
            publish_heard()
            events = drain(subscription)

        assert [event["kind"] for event in events] == ["heard"]
        assert events[0]["data"]["text"] == "wie ist das wetter"

    def test_it_names_the_turn_and_where_it_came_from(self):
        trace = get_recorder().begin(source="text")
        trace.transcript = "hallo"

        with get_event_bus().subscribe() as subscription:
            publish_heard()
            events = drain(subscription)

        assert events[0]["data"]["turn_id"] == trace.turn_id
        assert events[0]["data"]["source"] == "text"

    def test_an_empty_transcript_is_not_worth_publishing(self):
        get_recorder().begin(source="voice")

        with get_event_bus().subscribe() as subscription:
            publish_heard()

            assert drain(subscription) == []

    def test_outside_a_turn_it_says_nothing(self):
        with get_event_bus().subscribe() as subscription:
            publish_heard()

            assert drain(subscription) == []


class TestTheAnswerAsItIsWritten:
    def test_each_piece_reaches_a_watcher_in_order(self):
        trace = get_recorder().begin(source="voice")

        with get_event_bus().subscribe() as subscription:
            publish_reply("Das Wetter ")
            publish_reply("ist gut.")
            events = drain(subscription)

        assert [event["kind"] for event in events] == ["reply", "reply"]
        assert [event["data"]["delta"] for event in events] == ["Das Wetter ", "ist gut."]
        assert {event["data"]["turn_id"] for event in events} == {trace.turn_id}

    def test_an_empty_piece_is_not_published(self):
        get_recorder().begin(source="voice")

        with get_event_bus().subscribe() as subscription:
            publish_reply("")

            assert drain(subscription) == []

    def test_outside_a_turn_it_says_nothing(self):
        with get_event_bus().subscribe() as subscription:
            publish_reply("Hallo.")

            assert drain(subscription) == []
