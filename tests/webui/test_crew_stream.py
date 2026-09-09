"""The crew reading, taken once for everyone who is watching.

Mission Control used to be fetched by each open page on its own timer, so
two tabs meant twice the traffic to a NAS that is not always awake. The
daemon takes the reading instead and publishes it on the event bus every
page already listens to.

That puts a thread in the daemon that talks to the network, which is only
acceptable if it stays quiet: no endpoint configured or nobody watching
must mean no outbound request at all.
"""

from __future__ import annotations

import json

import pytest
import requests

from jarvis.runtime import EventBus, get_event_bus
from jarvis.webui.crew_stream import CrewPoller


ENTRY = {
    "id": 1,
    "agent_name": "DEV",
    "task_description": "Fixed the router",
    "model_used": "gpt-5.6-sol",
    "status": "success",
    "created_at": "2026-08-18T09:00:00+00:00",
}


class _OkResponse:
    def __init__(self, entries):
        self._entries = entries

    def raise_for_status(self):
        return None

    def json(self):
        return {"entries": self._entries}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A crew endpoint in the config, with no real host behind it."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "crew_api_url": "http://192.168.178.113:8643",
            "crew_api_key": "s3cret",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
    return config_path


@pytest.fixture
def unconfigured(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"crew_api_url": ""}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
    return config_path


@pytest.fixture
def bus():
    """A bus of this test's own.

    The daemon's bus is process-wide, so a stream left open by another test
    would silently count as a page that is watching, and "nobody is
    watching" could never be asserted. That the server hands the poller the
    real bus is a separate property, checked in TestLifecycle.
    """
    return EventBus()


@pytest.fixture
def calls(monkeypatch):
    """Every outbound request the poller attempts."""
    attempted = []

    def _get(url, headers=None, timeout=None):
        attempted.append(url)
        return _OkResponse([ENTRY])

    monkeypatch.setattr(requests, "get", _get)
    return attempted


def _drain(subscription):
    """Every event already queued for a subscriber, without waiting."""
    events = []
    for event in subscription.listen(timeout=0.01):
        if event is None:
            break
        events.append(event)
    return events


class TestSilenceWhenNobodyIsWatching:
    def test_no_request_is_made_while_no_page_is_listening(self, configured, calls, bus):
        CrewPoller(bus=bus).tick()

        assert calls == [], "the NAS was contacted with nobody watching"

    def test_no_request_is_made_while_no_endpoint_is_configured(
        self, unconfigured, calls, bus,
    ):
        with bus.subscribe():
            CrewPoller(bus=bus).tick()

        assert calls == []

    def test_an_unconfigured_endpoint_publishes_nothing(self, unconfigured, calls, bus):
        with bus.subscribe() as subscription:
            CrewPoller(bus=bus).tick()

            assert _drain(subscription) == []


class TestPublishing:
    def test_a_listening_page_receives_the_reading(self, configured, calls, bus):
        with bus.subscribe() as subscription:
            CrewPoller(bus=bus).tick()

            events = _drain(subscription)

        assert calls, "the NAS was never contacted"
        assert [event["kind"] for event in events] == ["crew"]
        published = events[0]["data"]
        assert published["reachable"] is True
        assert published["entries"] == [ENTRY]
        assert published["checked_at"] > 0

    def test_one_reading_serves_every_watcher(self, configured, calls, bus):
        with bus.subscribe() as first, bus.subscribe() as second:
            CrewPoller(bus=bus).tick()

            assert len(_drain(first)) == 1
            assert len(_drain(second)) == 1

        assert len(calls) == 1, "the NAS was polled once per watcher"

    def test_a_nas_that_does_not_answer_is_published_as_unreachable(
        self, configured, monkeypatch, bus,
    ):
        def _boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("no route to host")

        monkeypatch.setattr(requests, "get", _boom)

        with bus.subscribe() as subscription:
            CrewPoller(bus=bus).tick()

            events = _drain(subscription)

        assert len(events) == 1
        assert events[0]["data"] == {
            "configured": True,
            "reachable": False,
            "checked_at": events[0]["data"]["checked_at"],
            "entries": [],
            "agents": [],
            "daily": [],
        }

    def test_a_reading_that_fails_outright_does_not_stop_the_next_one(
        self, configured, monkeypatch, bus,
    ):
        broken = {"first": True}

        def _sometimes(*args, **kwargs):
            if broken["first"]:
                broken["first"] = False
                raise RuntimeError("something no caller expected")
            return _OkResponse([ENTRY])

        monkeypatch.setattr(requests, "get", _sometimes)
        poller = CrewPoller(bus=bus)

        with bus.subscribe() as subscription:
            poller.tick()
            poller.tick()

            events = _drain(subscription)

        assert events[-1]["data"]["reachable"] is True


class TestLifecycle:
    def test_the_control_centre_runs_the_poller_while_it_serves(self, configured):
        from jarvis.webui.server import WebUIConfig, WebUIServer

        server = WebUIServer(WebUIConfig(host="127.0.0.1", port=0, token=""))
        server.start()
        try:
            assert server.crew_poller is not None
            assert server.crew_poller.running is True
            # Onto the bus the event stream reads from, or no page sees it.
            assert server.crew_poller.bus is get_event_bus()
        finally:
            server.stop()

        assert server.crew_poller is None
