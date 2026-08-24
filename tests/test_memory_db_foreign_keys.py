"""Tests for foreign key enforcement on ``Database``'s SQLite connection.

``conversation_summaries`` rewrites (``upsert_conversation_summary``) go
through SQLite's ``INSERT OR REPLACE``, which deletes the old row and
inserts a brand new one with a new id whenever the ``UNIQUE(date_utc,
source_app)`` constraint conflicts. The ``summary_vec`` table declares
``ON DELETE CASCADE`` against ``conversation_summaries(id)`` so that old
row's vector-index entry disappears with it -- but only once
``PRAGMA foreign_keys = ON`` is actually in effect on the connection.

sqlite-vss isn't installed in this environment, so these tests stand up
the ``summary_vec``/``embeddings`` pair themselves with a plain table in
place of the real ``vss0`` virtual table. The foreign key relationship
under test (``summary_vec.summary_id`` -> ``conversation_summaries.id``
and ``summary_vec.emb_id`` -> ``embeddings.id``) is identical either way.
"""

from __future__ import annotations

from jarvis.memory.db import Database


def _make_vss_enabled_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "jarvis.db"), sqlite_vss_path=None)
    db.is_vss_enabled = True
    db.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
          id  INTEGER PRIMARY KEY,
          vec BLOB
        );

        CREATE TABLE IF NOT EXISTS summary_vec (
          summary_id INTEGER PRIMARY KEY REFERENCES conversation_summaries(id) ON DELETE CASCADE,
          emb_id     INTEGER NOT NULL REFERENCES embeddings(id)
        );
        """
    )
    db.conn.commit()
    return db


class TestForeignKeysPragma:
    def test_foreign_keys_pragma_is_on(self, tmp_path):
        db = Database(str(tmp_path / "jarvis.db"), sqlite_vss_path=None)
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


class TestSummaryVecCascade:
    def test_diary_rewrite_cascades_old_summary_vec_row(self, tmp_path):
        """The exact rewrite scenario from the bug report: rewriting a day's
        summary deletes the old ``conversation_summaries`` row and inserts a
        new one with a new id. With foreign keys enforced, the old row's
        ``summary_vec`` entry disappears immediately -- no separate cleanup
        call needed for it."""
        db = _make_vss_enabled_db(tmp_path)

        old_id = db.upsert_conversation_summary(
            "2026-08-24", "first version", source_app="jarvis",
        )
        old_emb_id = db.upsert_summary_embedding(old_id, [0.1] * 768)

        new_id = db.upsert_conversation_summary(
            "2026-08-24", "rewritten version", source_app="jarvis",
        )

        assert new_id != old_id, "INSERT OR REPLACE must assign a fresh id"
        assert db.conn.execute(
            "SELECT COUNT(*) FROM summary_vec WHERE summary_id = ?", (old_id,)
        ).fetchone()[0] == 0

        # The embeddings row is a step removed from conversation_summaries
        # (summary_vec -> embeddings has no cascade of its own), so it is
        # left as a fresh orphan -- nothing points at it any more -- until
        # the sweep below runs; this is exactly the gap the cleanup path
        # closes.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE id = ?", (old_emb_id,)
        ).fetchone()[0] == 1
        db._cleanup_orphaned_summary_embeddings()
        assert db.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE id = ?", (old_emb_id,)
        ).fetchone()[0] == 0

    def test_deleting_summary_cascades_summary_vec_row(self, tmp_path):
        db = _make_vss_enabled_db(tmp_path)

        summary_id = db.upsert_conversation_summary(
            "2026-08-24", "test summary", source_app="jarvis",
        )
        db.upsert_summary_embedding(summary_id, [0.1] * 768)

        db.conn.execute(
            "DELETE FROM conversation_summaries WHERE id = ?", (summary_id,)
        )
        db.conn.commit()

        assert db.conn.execute(
            "SELECT COUNT(*) FROM summary_vec WHERE summary_id = ?", (summary_id,)
        ).fetchone()[0] == 0


class TestOrphanedSummaryEmbeddingCleanup:
    def test_cleanup_removes_preexisting_orphans_without_touching_live_rows(self, tmp_path):
        db = _make_vss_enabled_db(tmp_path)

        live_id = db.upsert_conversation_summary(
            "2026-08-24", "live summary", source_app="jarvis",
        )
        live_emb_id = db.upsert_summary_embedding(live_id, [0.2] * 768)

        # Simulate an orphan pair left behind from before foreign key
        # enforcement was turned on: a summary_vec/embeddings row pair whose
        # summary_id has no matching conversation_summaries row.
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("INSERT INTO embeddings(id, vec) VALUES (9999, NULL)")
        db.conn.execute(
            "INSERT INTO summary_vec(summary_id, emb_id) VALUES (8888, 9999)"
        )
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = ON")

        removed = db._cleanup_orphaned_summary_embeddings()

        assert removed == 2  # one summary_vec row + its embeddings row
        assert db.conn.execute(
            "SELECT COUNT(*) FROM summary_vec WHERE summary_id = 8888"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE id = 9999"
        ).fetchone()[0] == 0

        # Live row is untouched.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM summary_vec WHERE summary_id = ?", (live_id,)
        ).fetchone()[0] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE id = ?", (live_emb_id,)
        ).fetchone()[0] == 1

    def test_cleanup_is_idempotent(self, tmp_path):
        db = _make_vss_enabled_db(tmp_path)

        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("INSERT INTO embeddings(id, vec) VALUES (9999, NULL)")
        db.conn.execute(
            "INSERT INTO summary_vec(summary_id, emb_id) VALUES (8888, 9999)"
        )
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = ON")

        assert db._cleanup_orphaned_summary_embeddings() == 2
        assert db._cleanup_orphaned_summary_embeddings() == 0
