"""Behaviour tests for the confirmation channel the control centre serves.

The gate refuses an action when no channel can answer. A setup that runs
the daemon on its own and watches it in a browser has no Qt tray, no
Telegram token, and speakers that are not in the room, so without this
channel every protected tool is refused. These tests cover the three ways a
request can end and the one condition that makes the channel available.
"""

import threading
import time

import pytest

from jarvis.security.gate import SecurityGate
from jarvis.security.web_confirm import WebConfirm, get_web_confirmations
from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}
WRITE_HEADERS = {**HEADERS, "X-Jarvis-UI": "1"}


@pytest.fixture(autouse=True)
def _clean_confirmations():
    get_web_confirmations().reset()
    yield
    get_web_confirmations().reset()
    SecurityGate.reset_instance()


@pytest.fixture
def client():
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


def _ask_in_background(action="deleteMeal", args=None, timeout=5):
    """Start a confirmation request and return (thread, results list)."""
    results: list[bool] = []
    channel = WebConfirm(timeout_seconds=timeout)

    def _run():
        results.append(channel.ask(action, args or {}))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, results


def _wait_for_pending(client, attempts=100):
    for _ in range(attempts):
        pending = client.get("/api/security/pending", headers=HEADERS).get_json()["pending"]
        if pending:
            return pending
        time.sleep(0.01)
    return []


class TestAvailability:
    def test_the_channel_is_absent_until_a_page_could_show_a_request(self):
        assert WebConfirm().is_available is False

    def test_the_channel_appears_once_the_control_centre_serves(self):
        get_web_confirmations().set_serving(True)

        assert WebConfirm().is_available is True


class TestDecisions:
    def test_approving_lets_the_action_run(self, client):
        get_web_confirmations().set_serving(True)
        thread, results = _ask_in_background()

        pending = _wait_for_pending(client)
        assert len(pending) == 1
        client.post("/api/security/decide", headers=WRITE_HEADERS, json={
            "request_id": pending[0]["request_id"], "approved": True,
        })
        thread.join(timeout=5)

        assert results == [True]

    def test_refusing_stops_the_action(self, client):
        get_web_confirmations().set_serving(True)
        thread, results = _ask_in_background()

        pending = _wait_for_pending(client)
        client.post("/api/security/decide", headers=WRITE_HEADERS, json={
            "request_id": pending[0]["request_id"], "approved": False,
        })
        thread.join(timeout=5)

        assert results == [False]

    def test_no_answer_in_time_counts_as_a_refusal(self):
        get_web_confirmations().set_serving(True)
        channel = WebConfirm(timeout_seconds=0)

        assert channel.ask("deleteMeal", {}) is False

    def test_a_request_carries_what_would_run(self, client):
        get_web_confirmations().set_serving(True)
        thread, _ = _ask_in_background(action="localFiles", args={"operation": "delete"})

        pending = _wait_for_pending(client)

        assert pending[0]["action_name"] == "localFiles"
        assert pending[0]["action_args"] == {"operation": "delete"}
        client.post("/api/security/decide", headers=WRITE_HEADERS, json={
            "request_id": pending[0]["request_id"], "approved": False,
        })
        thread.join(timeout=5)

    def test_answering_twice_is_refused_the_second_time(self, client):
        get_web_confirmations().set_serving(True)
        thread, _ = _ask_in_background()
        pending = _wait_for_pending(client)
        request_id = pending[0]["request_id"]

        client.post("/api/security/decide", headers=WRITE_HEADERS, json={
            "request_id": request_id, "approved": True,
        })
        thread.join(timeout=5)
        second = client.post("/api/security/decide", headers=WRITE_HEADERS, json={
            "request_id": request_id, "approved": True,
        })

        assert second.status_code == 404


class TestDecisionLog:
    def test_every_outcome_is_written_down(self):
        get_web_confirmations().set_serving(True)
        WebConfirm(timeout_seconds=0).ask("deleteMeal", {})

        log = get_web_confirmations().decisions()

        assert log[-1]["action_name"] == "deleteMeal"
        assert log[-1]["outcome"] == "timed out"

    def test_the_log_is_readable_from_the_overview(self, client):
        get_web_confirmations().set_serving(True)
        WebConfirm(timeout_seconds=0).ask("deleteMeal", {})

        body = client.get("/api/security", headers=HEADERS).get_json()

        assert body["decisions"][-1]["outcome"] == "timed out"


class TestTheGateUsesIt:
    def test_the_channel_is_in_the_gate_s_default_order(self):
        from jarvis.config import get_default_config

        assert "web" in get_default_config()["security_confirm_channels"]

    def test_a_protected_tool_is_approved_through_the_browser(self, client):
        get_web_confirmations().set_serving(True)
        gate = SecurityGate(level="critical", channels={"web": WebConfirm(timeout_seconds=5)},
                            confirm_channels=["web"])
        results: list[bool] = []

        thread = threading.Thread(
            target=lambda: results.append(gate.confirm("deleteMeal", {})), daemon=True,
        )
        thread.start()
        pending = _wait_for_pending(client)
        client.post("/api/security/decide", headers=WRITE_HEADERS, json={
            "request_id": pending[0]["request_id"], "approved": True,
        })
        thread.join(timeout=5)

        assert results == [True]
