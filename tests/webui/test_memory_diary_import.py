"""Tests for diary-to-graph import feature.

Covers:
- Database.get_all_conversation_summaries() method
- /api/graph/import-diary streaming endpoint (requires flask)
"""

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from jarvis.memory.db import Database


# ── Database method tests ─────────────────────────────────────────────


@pytest.fixture
def db_with_summaries(tmp_path):
    """Provide a database pre-populated with conversation summaries."""
    db = Database(str(tmp_path / "test.db"), sqlite_vss_path=None)

    # Insert some summaries in non-chronological order to test ordering
    summaries = [
        ("2025-03-15", "User discussed work projects and deadlines.", "work,planning", "jarvis"),
        ("2025-01-10", "User talked about favourite coffee shops.", "food,coffee", "jarvis"),
        ("2025-06-22", "User mentioned upcoming holiday plans.", "travel,holiday", "jarvis"),
        ("2025-02-01", "User shared fitness routine details.", "health,fitness", "jarvis"),
    ]

    for date_utc, summary, topics, source_app in summaries:
        ts_utc = datetime.now(timezone.utc).isoformat()
        db.conn.execute(
            """INSERT INTO conversation_summaries (date_utc, ts_utc, summary, topics, source_app)
               VALUES (?, ?, ?, ?, ?)""",
            (date_utc, ts_utc, summary, topics, source_app),
        )
    db.conn.commit()

    yield db
    db.close()


@pytest.mark.unit
class TestGetAllConversationSummaries:
    """Tests for Database.get_all_conversation_summaries()."""

    def test_returns_all_summaries(self, db_with_summaries):
        """Should return every summary in the database."""
        rows = db_with_summaries.get_all_conversation_summaries()
        assert len(rows) == 4

    def test_ordered_by_date_ascending(self, db_with_summaries):
        """Summaries should be ordered oldest-first for chronological import."""
        rows = db_with_summaries.get_all_conversation_summaries()
        dates = [row["date_utc"] for row in rows]
        assert dates == sorted(dates)
        assert dates[0] == "2025-01-10"
        assert dates[-1] == "2025-06-22"

    def test_empty_database(self, db):
        """Should return an empty list when no summaries exist."""
        rows = db.get_all_conversation_summaries()
        assert rows == []

    def test_returns_expected_fields(self, db_with_summaries):
        """Each row should have the standard conversation_summaries fields."""
        rows = db_with_summaries.get_all_conversation_summaries()
        row = rows[0]
        assert "date_utc" in row.keys()
        assert "summary" in row.keys()
        assert "topics" in row.keys()
        assert "source_app" in row.keys()

    def test_contains_summary_text(self, db_with_summaries):
        """Summaries should contain the actual text that was stored."""
        rows = db_with_summaries.get_all_conversation_summaries()
        texts = [row["summary"] for row in rows]
        assert any("coffee" in t for t in texts)
        assert any("fitness" in t for t in texts)


# ── Import endpoint tests ─────────────────────────────────────────────

try:
    import flask as _flask  # noqa: F401
    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


@pytest.mark.unit
@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestImportDiaryEndpoint:
    """Tests for /api/graph/import-diary streaming endpoint."""

    @pytest.fixture(autouse=True)
    def setup_app(self, tmp_path):
        """Set up Flask test client with a temporary database."""
        from jarvis.memory.graph import GraphMemoryStore
        from jarvis.webui.api import memory as memory_api
        from tests.webui.conftest import control_centre_client

        self.db_path = str(tmp_path / "test.db")

        # Create database with summaries
        self.db = Database(self.db_path, sqlite_vss_path=None)
        self.db.conn.execute(
            """INSERT INTO conversation_summaries (date_utc, ts_utc, summary, topics, source_app)
               VALUES (?, ?, ?, ?, ?)""",
            ("2025-03-15", "2025-03-15T12:00:00Z", "User likes dark roast coffee.", "food", "jarvis"),
        )
        self.db.conn.execute(
            """INSERT INTO conversation_summaries (date_utc, ts_utc, summary, topics, source_app)
               VALUES (?, ?, ?, ?, ?)""",
            ("2025-03-16", "2025-03-16T12:00:00Z", "User works at Acme Corp.", "work", "jarvis"),
        )
        self.db.conn.commit()

        # ``get_graph_store`` is process-global. Patch decorators below alter
        # ``_get_db_path`` only while a test runs, but a store cached by an
        # earlier Web UI test would otherwise win and can point at the real
        # default database. Inject and own the temporary store explicitly.
        self.graph_store = GraphMemoryStore(self.db_path)
        memory_api._graph_store = self.graph_store
        self.client = control_centre_client()

        yield
        self.db.close()
        self.graph_store.close()
        memory_api._graph_store = None

    def _parse_ndjson(self, data: bytes) -> list[dict]:
        """Parse newline-delimited JSON from response data."""
        lines = data.decode("utf-8").strip().split("\n")
        return [json.loads(line) for line in lines if line.strip()]

    @patch("jarvis.webui.api.memory._get_db_path")
    @patch("jarvis.webui.api.memory.load_settings")
    @patch("jarvis.memory.graph_ops.call_llm_direct")
    def test_import_streams_progress(self, mock_llm, mock_settings, mock_db_path):
        """Should stream start, progress, and complete messages."""
        mock_db_path.return_value = self.db_path

        cfg = MagicMock()
        cfg.ollama_base_url = "http://localhost:11434"
        cfg.ollama_chat_model = "test-model"
        cfg.llm_chat_model = "test-model"
        cfg.llm_chat_timeout_sec = 10.0
        cfg.llm_thinking_enabled = False
        mock_settings.return_value = cfg

        # Each empty fixed branch accepts its first fact directly, so these
        # two summaries need one branch-tagged extraction response each.
        mock_llm.side_effect = [
            '[{"branch": "USER", "fact": "Likes dark roast coffee"}]',
            '[{"branch": "USER", "fact": "Works at Acme Corp"}]',
        ]

        resp = self.client.post("/api/graph/import-diary")
        assert resp.status_code == 200

        messages = self._parse_ndjson(resp.data)
        types = [m["type"] for m in messages]

        assert "start" in types
        assert "progress" in types
        assert "complete" in types

        start_msg = next(m for m in messages if m["type"] == "start")
        assert start_msg["total"] == 2

        complete_msg = next(m for m in messages if m["type"] == "complete")
        assert complete_msg["processed"] == 2
        assert "dark roast coffee" in self.graph_store.get_node("user").data.lower()
        assert "acme corp" in self.graph_store.get_node("user").data.lower()

    @patch("jarvis.webui.api.memory._get_db_path")
    @patch("jarvis.webui.api.memory.load_settings")
    def test_import_empty_diary(self, mock_settings, mock_db_path, tmp_path):
        """Should handle empty diary gracefully."""
        empty_db_path = str(tmp_path / "empty.db")
        empty_db = Database(empty_db_path, sqlite_vss_path=None)
        mock_db_path.return_value = empty_db_path

        cfg = MagicMock()
        mock_settings.return_value = cfg

        resp = self.client.post("/api/graph/import-diary")
        messages = self._parse_ndjson(resp.data)

        assert len(messages) == 1
        assert messages[0]["type"] == "complete"
        assert messages[0]["processed"] == 0

        empty_db.close()

    @patch("jarvis.webui.api.memory._get_db_path")
    @patch("jarvis.webui.api.memory.load_settings")
    @patch("jarvis.memory.graph_ops.call_llm_direct")
    def test_import_continues_on_per_summary_error(self, mock_llm, mock_settings, mock_db_path):
        """If one summary fails, the import should continue with the rest."""
        mock_db_path.return_value = self.db_path

        cfg = MagicMock()
        cfg.ollama_base_url = "http://localhost:11434"
        cfg.ollama_chat_model = "test-model"
        cfg.llm_chat_model = "test-model"
        cfg.llm_chat_timeout_sec = 10.0
        cfg.llm_thinking_enabled = False
        mock_settings.return_value = cfg

        # First summary extraction fails, second succeeds
        mock_llm.side_effect = [
            None,
            '[{"branch": "USER", "fact": "Works at Acme Corp"}]',
        ]

        resp = self.client.post("/api/graph/import-diary")
        messages = self._parse_ndjson(resp.data)

        progress_msgs = [m for m in messages if m["type"] == "progress"]
        assert len(progress_msgs) == 2  # Both summaries processed

        complete_msg = next(m for m in messages if m["type"] == "complete")
        assert complete_msg["processed"] == 2
