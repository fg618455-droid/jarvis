"""The Control Centre exposes a local, redacted diagnostic log."""

from __future__ import annotations


def test_recent_logs_are_available_and_redact_credentials(api_client):
    from jarvis.debug import clear_recent_logs, debug_log

    clear_recent_logs()
    debug_log("provider rejected api_key=synthetic-secret-value", "voice")

    response = api_client.get("/api/logs?limit=10")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["entries"]
    assert "synthetic-secret-value" not in response.get_data(as_text=True)
    assert payload["entries"][-1]["category"] == "voice"
