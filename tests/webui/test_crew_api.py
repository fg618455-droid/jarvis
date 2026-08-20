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


HEADERS = {"Host": "127.0.0.1:5055", "X-Jarvis-UI": "1"}


@pytest.fixture
def client():
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


def _configure(tmp_path, monkeypatch, url="http://192.168.178.113:8643", key="s3cret"):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"crew_api_url": url, "crew_api_key": key}), encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))


class TestUnconfigured:
    def test_an_empty_url_is_reported_as_not_configured(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, url="")

        body = client.get("/api/crew", headers=HEADERS).get_json()

        assert body == {
            "configured": False, "reachable": False, "entries": [], "agents": [], "daily": [],
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

        assert response.status_code == 200
        assert response.get_json() == {
            "configured": True, "reachable": False, "entries": [], "agents": [], "daily": [],
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
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._OkResponse(self.ENTRIES),
        )

        body = client.get("/api/crew", headers=HEADERS).get_json()

        by_name = {agent["name"]: agent for agent in body["agents"]}
        assert by_name["DEV"] == {"name": "DEV", "success": 1, "failure": 1, "partial": 0}
        assert by_name["RESEARCH"] == {"name": "RESEARCH", "success": 0, "failure": 0, "partial": 1}

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


class _OkPostResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestChat:
    def test_an_unconfigured_endpoint_never_makes_a_request(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch, url="")
        calls = []
        monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1))

        response = client.post(
            "/api/crew/chat", json={"agent": "dev", "message": "status?"}, headers=HEADERS,
        )

        assert calls == []
        assert response.status_code == 200
        assert response.get_json()["reachable"] is False

    def test_an_unknown_agent_is_rejected_without_a_request(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1))

        response = client.post(
            "/api/crew/chat", json={"agent": "nobody", "message": "hi"}, headers=HEADERS,
        )

        assert response.status_code == 400
        assert calls == []

    def test_an_empty_message_is_rejected_without_a_request(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1))

        response = client.post(
            "/api/crew/chat", json={"agent": "dev", "message": "   "}, headers=HEADERS,
        )

        assert response.status_code == 400
        assert calls == []

    def test_a_reply_is_forwarded(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: _OkPostResponse({"reply": "on it"}),
        )

        response = client.post(
            "/api/crew/chat", json={"agent": "dev", "message": "status?"}, headers=HEADERS,
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["reachable"] is True
        assert body["reply"] == "on it"

    def test_the_agent_message_and_key_ride_along(self, client, tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, key="topsecret")
        seen = {}

        def _post(url, headers=None, json=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return _OkPostResponse({"reply": "ok"})

        monkeypatch.setattr(requests, "post", _post)

        client.post(
            "/api/crew/chat", json={"agent": "research", "message": "check timetable"},
            headers=HEADERS,
        )

        assert seen["url"] == "http://192.168.178.113:8643/chat"
        assert seen["headers"]["X-Crew-Key"] == "topsecret"
        assert seen["json"] == {"agent": "research", "message": "check timetable"}

    def test_a_connection_failure_is_reported_without_a_500(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)

        def _boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("no route to host")

        monkeypatch.setattr(requests, "post", _boom)

        response = client.post(
            "/api/crew/chat", json={"agent": "dev", "message": "hi"}, headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.get_json()["reachable"] is False

    def test_an_upstream_error_is_reported_without_a_500(
        self, client, tmp_path, monkeypatch,
    ):
        _configure(tmp_path, monkeypatch)
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: _OkPostResponse({"error": "chat disabled"}, status_code=503),
        )

        response = client.post(
            "/api/crew/chat", json={"agent": "dev", "message": "hi"}, headers=HEADERS,
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["reachable"] is False
