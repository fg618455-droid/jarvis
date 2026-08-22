"""Behaviour tests for the Mission Control endpoint.

The NAS the crew runs on is not always reachable, and the endpoint must say
so plainly rather than fail the whole view. Each case is exercised through a
real request against the blueprint, with the outbound call to the NAS
mocked, because the guard wiring and the JSON shape are what would break
silently otherwise.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}


@pytest.fixture
def client():
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


def _configure(
    tmp_path, monkeypatch, url="http://192.168.178.113:8643", key="s3cret", agents=None,
):
    config_path = tmp_path / "config.json"
    stored = {"crew_api_url": url, "crew_api_key": key}
    if agents is not None:
        stored["crew_agents"] = agents
    config_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))


class TestUnconfigured:
    def test_an_empty_url_is_reported_as_not_configured(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, url="")

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body == {
            "configured": False, "reachable": False, "checked_at": body["checked_at"],
            "entries": [], "agents": [], "daily": [],
        }

    def test_an_unconfigured_endpoint_never_makes_a_request(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, url="")
        calls = []
        monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1))

        client.get("/api/crew", headers=HEADERS)

        assert calls == []


class TestUnreachable:
    def test_a_connection_failure_is_reported_without_a_500(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)

        def _boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("no route to host")

        monkeypatch.setattr(requests, "get", _boom)

        response = client.get("/api/crew", headers=HEADERS)

        body = response.get_json()
        assert response.status_code == 200
        assert body == {
            "configured": True, "reachable": False, "checked_at": body["checked_at"],
            "entries": [], "agents": [], "daily": [],
        }

    def test_a_timeout_is_reported_without_a_500(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.Timeout()),
        )

        response = client.get("/api/crew", headers=HEADERS)

        assert response.status_code == 200
        assert response.get_json()["reachable"] is False

    def test_a_non_json_reply_is_reported_as_unreachable(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)

        class _BadResponse:
            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(requests, "get", lambda *a, **k: _BadResponse())

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body["reachable"] is False


class TestReachable:
    ENTRIES = [
        {"id": 3, "agent_name": "DEV", "task_description": "Fixed the router",
         "model_used": "gpt-5.6-sol", "status": "success", "created_at": "2026-08-18T09:00:00+00:00"},
        {"id": 2, "agent_name": "DEV", "task_description": "Broke the router",
         "model_used": "gpt-5.6-sol", "status": "failure", "created_at": "2026-08-18T08:00:00+00:00"},
        {"id": 1, "agent_name": "RESEARCH", "task_description": "Checked the RAM",
         "model_used": "gemini-2.5-flash", "status": "partial", "created_at": "2026-08-18T07:00:00+00:00"},
    ]

    class _OkResponse:
        def __init__(self, entries):
            self._entries = entries

        def raise_for_status(self):
            return None

        def json(self):
            return {"entries": self._entries}

    def test_entries_are_passed_through(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._OkResponse(self.ENTRIES),
        )

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body["configured"] is True
        assert body["reachable"] is True
        assert body["entries"] == self.ENTRIES

    def test_agents_are_tallied_by_status(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, agents=["DEV", "RESEARCH"])
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._OkResponse(self.ENTRIES),
        )

        body = client.get("/api/crew", headers=HEADERS).get_json()

        by_name = {agent["name"]: agent for agent in body["agents"]}
        assert by_name["DEV"]["success"] == 1
        assert by_name["DEV"]["failure"] == 1
        assert by_name["DEV"]["partial"] == 0
        assert by_name["DEV"]["total"] == 2
        assert by_name["RESEARCH"]["partial"] == 1
        assert by_name["RESEARCH"]["total"] == 1

    def test_the_configured_key_rides_along_as_a_header(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, key="topsecret")
        seen = {}

        def _get(url, headers=None, timeout=None):
            seen["headers"] = headers
            return self._OkResponse([])

        monkeypatch.setattr(requests, "get", _get)

        client.get("/api/crew", headers=HEADERS)

        assert seen["headers"]["X-Crew-Key"] == "topsecret"

    def test_a_limit_is_forwarded_to_the_nas_endpoint(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch)
        seen = {}

        def _get(url, headers=None, timeout=None):
            seen["url"] = url
            return self._OkResponse([])

        monkeypatch.setattr(requests, "get", _get)

        client.get("/api/crew?limit=50", headers=HEADERS)

        assert "limit=50" in seen["url"]


class TestTheRoster:
    """A crew of seven with one busy agent is not a crew of one.

    The endpoint only ever hears from agents that logged something, so an
    idle agent would vanish from the view entirely and read as though it
    did not exist. The configured roster decides who is shown; the log
    decides what is said about them.
    """

    ROSTER = ["JARVIS", "DEV", "RESEARCH", "ASSISTANT", "SCHULE", "SCRIBE", "REACH"]

    def _serve(self, monkeypatch, entries):
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: TestReachable._OkResponse(entries),
        )

    def test_the_whole_roster_is_reported_when_only_one_agent_has_worked(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, agents=self.ROSTER)
        self._serve(monkeypatch, TestReachable.ENTRIES)

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert [agent["name"] for agent in body["agents"]] == self.ROSTER

    def test_an_agent_that_has_logged_nothing_reads_as_empty_not_missing(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, agents=self.ROSTER)
        self._serve(monkeypatch, TestReachable.ENTRIES)

        body = client.get("/api/crew", headers=HEADERS).get_json()
        scribe = next(a for a in body["agents"] if a["name"] == "SCRIBE")

        assert scribe["total"] == 0
        assert scribe["last_at"] is None
        assert scribe["last_status"] is None

    def test_an_agent_outside_the_roster_is_still_reported(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, agents=["DEV"])
        self._serve(monkeypatch, TestReachable.ENTRIES)

        body = client.get("/api/crew", headers=HEADERS).get_json()

        names = [agent["name"] for agent in body["agents"]]
        assert names == ["DEV", "RESEARCH"], "an unlisted agent was hidden"

    def test_an_agent_carries_its_most_recent_outcome(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, agents=["DEV"])
        self._serve(monkeypatch, TestReachable.ENTRIES)

        body = client.get("/api/crew", headers=HEADERS).get_json()
        dev = next(a for a in body["agents"] if a["name"] == "DEV")

        assert dev["last_at"] == "2026-08-18T09:00:00+00:00"
        assert dev["last_status"] == "success"

    def test_each_agent_carries_its_own_counts_over_the_shared_window(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, agents=["DEV", "RESEARCH"])
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        self._serve(monkeypatch, [
            {"id": 1, "agent_name": "DEV", "task_description": "a", "model_used": "m",
             "status": "success", "created_at": f"{today.isoformat()}T09:00:00+00:00"},
            {"id": 2, "agent_name": "RESEARCH", "task_description": "b", "model_used": "m",
             "status": "success", "created_at": f"{yesterday.isoformat()}T09:00:00+00:00"},
        ])

        body = client.get("/api/crew", headers=HEADERS).get_json()
        by_name = {agent["name"]: agent for agent in body["agents"]}

        assert len(by_name["DEV"]["daily"]) == len(body["daily"])
        assert by_name["DEV"]["daily"][-1] == 1
        assert by_name["DEV"]["daily"][-2] == 0
        assert by_name["RESEARCH"]["daily"][-1] == 0
        assert by_name["RESEARCH"]["daily"][-2] == 1


class TestFreshness:
    """A reading has to say when it was taken.

    Status and freshness are different facts. An agent card that says
    "success" while the number behind it is an hour stale is worse than one
    that admits the age of what it is showing.
    """

    @pytest.mark.parametrize("url", ["", "http://192.168.178.113:8643"])
    def test_every_reply_says_when_it_was_taken(
        self, client, tmp_path, monkeypatch, url,
    ):
        _configure(tmp_path, monkeypatch, url=url)
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: TestReachable._OkResponse([]),
        )
        before = datetime.now(timezone.utc).timestamp()

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body["checked_at"] >= before

    def test_an_unreachable_nas_still_dates_its_reading(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.Timeout()),
        )
        before = datetime.now(timezone.utc).timestamp()

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body["reachable"] is False
        assert body["checked_at"] >= before


class TestDailyActivity:
    def test_entries_are_bucketed_by_calendar_day_over_a_fixed_window(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        entries = [
            {"id": 1, "agent_name": "DEV", "task_description": "a", "model_used": "m",
             "status": "success", "created_at": f"{today.isoformat()}T09:00:00+00:00"},
            {"id": 2, "agent_name": "DEV", "task_description": "b", "model_used": "m",
             "status": "success", "created_at": f"{today.isoformat()}T10:00:00+00:00"},
            {"id": 3, "agent_name": "RESEARCH", "task_description": "c", "model_used": "m",
             "status": "failure", "created_at": f"{yesterday.isoformat()}T10:00:00+00:00"},
        ]
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: TestReachable._OkResponse(entries),
        )

        body = client.get("/api/crew", headers=HEADERS).get_json()
        daily = body["daily"]
        by_date = {day["date"]: day["count"] for day in daily}

        assert len(daily) == 14
        assert daily[-1]["date"] == today.isoformat()
        assert by_date[today.isoformat()] == 2
        assert by_date[yesterday.isoformat()] == 1

    def test_a_day_is_split_by_outcome_not_only_counted(
        self, client, tmp_path, monkeypatch,
    ):
        """A busy day and a day of failures are not the same reading."""
        _configure(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).date().isoformat()
        monkeypatch.setattr(requests, "get", lambda *a, **k: TestReachable._OkResponse([
            {"id": 1, "agent_name": "DEV", "task_description": "a", "model_used": "m",
             "status": "success", "created_at": f"{today}T09:00:00+00:00"},
            {"id": 2, "agent_name": "DEV", "task_description": "b", "model_used": "m",
             "status": "failure", "created_at": f"{today}T10:00:00+00:00"},
            {"id": 3, "agent_name": "DEV", "task_description": "c", "model_used": "m",
             "status": "partial", "created_at": f"{today}T11:00:00+00:00"},
        ]))

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body["daily"][-1] == {
            "date": today, "count": 3, "success": 1, "failure": 1, "partial": 1,
        }

    def test_days_without_entries_are_zero_filled(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(requests, "get", lambda *a, **k: TestReachable._OkResponse([]))

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert len(body["daily"]) == 14
        assert all(day["count"] == 0 for day in body["daily"])

    def test_entries_outside_the_window_are_dropped(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch)
        long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        entries = [
            {"id": 1, "agent_name": "DEV", "task_description": "old", "model_used": "m",
             "status": "success", "created_at": long_ago},
        ]
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: TestReachable._OkResponse(entries),
        )

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert sum(day["count"] for day in body["daily"]) == 0
