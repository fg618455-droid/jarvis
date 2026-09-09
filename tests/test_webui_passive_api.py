"""Control-centre API behaviours for the passive transcript record."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jarvis.config import _save_json
from jarvis.listening.passive_capture import (
    register_passive_buffer,
    set_passive_capture_enabled,
)
from jarvis.listening.transcript_buffer import TranscriptBuffer
from jarvis.memory.db import Database
from jarvis.webui.server import WebUIConfig, create_app


@pytest.fixture
def passive_api(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    db_path = tmp_path / "jarvis.db"
    _save_json(config_path, {"db_path": str(db_path)})
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
    set_passive_capture_enabled(False)
    register_passive_buffer(None)

    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    client = app.test_client()
    headers = {"Host": "127.0.0.1:5055", "X-Jarvis-UI": "1"}

    database = Database(str(db_path))
    yield client, headers, database, config_path
    database.close()
    set_passive_capture_enabled(False)
    register_passive_buffer(None)


def _add(database, text, date="2026-08-11"):
    return database.insert_passive_transcript(
        ts_utc=f"{date}T09:00:00+00:00",
        date_utc=date,
        duration_sec=1.0,
        text=text,
        language="en",
        addressed=False,
        source_app="jarvis",
    )


class TestPassiveApi:
    def test_listing_filters_by_day_and_reports_undigested(self, passive_api):
        client, headers, database, _path = passive_api
        _add(database, "first day speech", "2026-08-10")
        _add(database, "second day speech", "2026-08-11")

        response = client.get("/api/passive?date=2026-08-11&limit=10", headers=headers)

        assert response.status_code == 200
        payload = response.get_json()
        assert [line["text"] for line in payload["lines"]] == ["second day speech"]
        assert payload["undigested_count"] == 2
        assert payload["enabled"] is False

    def test_switch_is_live_and_persisted(self, passive_api):
        client, headers, _database, config_path = passive_api

        response = client.post(
            "/api/passive/enabled", json={"enabled": True}, headers=headers
        )

        assert response.status_code == 200
        assert response.get_json()["enabled"] is True
        assert json.loads(config_path.read_text(encoding="utf-8"))[
            "passive_capture_enabled"
        ] is True

    def test_deletes_one_line(self, passive_api):
        client, headers, database, _path = passive_api
        line_id = _add(database, "delete only this line")
        _add(database, "keep this other line")

        response = client.delete(f"/api/passive/{line_id}", headers=headers)

        assert response.status_code == 200
        assert [row["text"] for row in database.list_passive_transcripts()] == [
            "keep this other line"
        ]

    def test_deletes_one_day(self, passive_api):
        client, headers, database, _path = passive_api
        _add(database, "old day line", "2026-08-10")
        _add(database, "new day line", "2026-08-11")

        response = client.delete("/api/passive?date=2026-08-10", headers=headers)

        assert response.status_code == 200
        assert [row["text"] for row in database.list_passive_transcripts()] == [
            "new day line"
        ]

    def test_deletes_all_and_clears_the_live_buffer(self, passive_api):
        client, headers, database, _path = passive_api
        _add(database, "stored line to delete")
        buffer = TranscriptBuffer()
        now = datetime.now(timezone.utc).timestamp()
        buffer.add("live line to delete", now - 1, now)
        register_passive_buffer(buffer)

        response = client.delete("/api/passive?all=1", headers=headers)

        assert response.status_code == 200
        assert database.list_passive_transcripts() == []
        assert len(buffer) == 0

    def test_write_routes_require_the_control_centre_header(self, passive_api):
        client, _headers, database, _path = passive_api
        line_id = _add(database, "protected line")

        response = client.delete(
            f"/api/passive/{line_id}", headers={"Host": "127.0.0.1:5055"}
        )

        assert response.status_code == 403
        assert len(database.list_passive_transcripts()) == 1

