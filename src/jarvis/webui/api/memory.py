"""🧠 Memory: the graph, the diary, and the meal log.

Reads and writes the same stores the assistant uses, so a node edited here
is the node the next reply reads. The graph is the point: three fixed
branches under a root, each node carrying its own access count and age, so
what the assistant actually leans on is visible and what has gone stale is
too.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request, Response

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.memory.graph import FIXED_BRANCH_IDS, GraphMemoryStore
from jarvis.memory.write_lock import MEMORY_WRITE_LOCK


# Routes carry their own /api prefix, kept verbatim so existing callers
# and bookmarks keep working.
bp = Blueprint("memory", __name__)

# Global database connection
_db_conn: Optional[sqlite3.Connection] = None
_graph_store: Optional[GraphMemoryStore] = None


def _get_db_path() -> str:
    """Get the database path from settings."""
    try:
        settings = load_settings()
        return settings.db_path
    except Exception:
        # Fallback to default path
        base = Path.home() / ".local" / "share" / "jarvis"
        return str(base / "jarvis.db")


def get_db() -> sqlite3.Connection:
    """Get or create database connection."""
    global _db_conn
    if _db_conn is None:
        db_path = _get_db_path()
        _db_conn = sqlite3.connect(db_path, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
    return _db_conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert sqlite3.Row to dictionary."""
    return {key: row[key] for key in row.keys()}


# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/api/memories")
def get_memories() -> Response:
    """
    Get all conversation summaries with optional filtering.

    Query params:
    - search: Search query for full-text search
    - topic: Filter by topic (comma-separated for multiple)
    - from_date: Start date (YYYY-MM-DD)
    - to_date: End date (YYYY-MM-DD)
    - limit: Max results (default 100)
    """
    conn = get_db()
    cur = conn.cursor()

    search = request.args.get("search", "").strip()
    topic_filter = request.args.get("topic", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)

    params: list[Any] = []
    conditions: list[str] = []

    # Build query based on filters
    if search:
        # Use FTS for search
        conditions.append("cs.id IN (SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH ?)")
        params.append(search)

    if topic_filter:
        # Filter by topic(s)
        topics = [t.strip().lower() for t in topic_filter.split(",") if t.strip()]
        if topics:
            topic_conditions = " OR ".join(["LOWER(cs.topics) LIKE ?" for _ in topics])
            conditions.append(f"({topic_conditions})")
            params.extend([f"%{t}%" for t in topics])

    if from_date:
        conditions.append("cs.date_utc >= ?")
        params.append(from_date)

    if to_date:
        conditions.append("cs.date_utc <= ?")
        params.append(to_date)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT cs.id, cs.date_utc, cs.ts_utc, cs.summary, cs.topics, cs.source_app
        FROM conversation_summaries cs
        WHERE {where_clause}
        ORDER BY cs.date_utc DESC
        LIMIT ?
    """
    params.append(limit)

    try:
        rows = cur.execute(query, params).fetchall()
        memories = [row_to_dict(row) for row in rows]

        # Parse topics into arrays
        for memory in memories:
            if memory.get("topics"):
                memory["topics_list"] = [t.strip() for t in memory["topics"].split(",") if t.strip()]
            else:
                memory["topics_list"] = []

        return jsonify({"memories": memories, "count": len(memories)})
    except Exception as e:
        return jsonify({"error": str(e), "memories": [], "count": 0}), 500


@bp.route("/api/topics")
def get_topics() -> Response:
    """Get all unique topics with their counts."""
    conn = get_db()
    cur = conn.cursor()

    try:
        rows = cur.execute("""
            SELECT topics FROM conversation_summaries WHERE topics IS NOT NULL AND topics != ''
        """).fetchall()

        topic_counts: dict[str, int] = {}
        for row in rows:
            topics_str = row["topics"]
            for topic in topics_str.split(","):
                topic = topic.strip().lower()
                if topic:
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1

        # Sort by count descending
        sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)

        return jsonify({
            "topics": [{"name": name, "count": count} for name, count in sorted_topics]
        })
    except Exception as e:
        return jsonify({"error": str(e), "topics": []}), 500


@bp.route("/api/meals")
def get_meals() -> Response:
    """
    Get meal logs with optional date filtering.

    Query params:
    - from_date: Start date (YYYY-MM-DD)
    - to_date: End date (YYYY-MM-DD)
    - limit: Max results (default 100)
    """
    conn = get_db()
    cur = conn.cursor()

    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)

    params: list[Any] = []
    conditions: list[str] = []

    if from_date:
        conditions.append("date(ts_utc) >= ?")
        params.append(from_date)

    if to_date:
        conditions.append("date(ts_utc) <= ?")
        params.append(to_date)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT * FROM meals
        WHERE {where_clause}
        ORDER BY ts_utc DESC
        LIMIT ?
    """
    params.append(limit)

    try:
        rows = cur.execute(query, params).fetchall()
        meals = [row_to_dict(row) for row in rows]
        return jsonify({"meals": meals, "count": len(meals)})
    except Exception as e:
        return jsonify({"error": str(e), "meals": [], "count": 0}), 500


@bp.route("/api/stats")
def get_stats() -> Response:
    """Get memory statistics."""
    conn = get_db()
    cur = conn.cursor()

    try:
        # Total memories
        total_memories = cur.execute("SELECT COUNT(*) as count FROM conversation_summaries").fetchone()["count"]

        # Date range
        date_range = cur.execute("""
            SELECT MIN(date_utc) as earliest, MAX(date_utc) as latest
            FROM conversation_summaries
        """).fetchone()

        # Memories by month
        monthly_stats = cur.execute("""
            SELECT strftime('%Y-%m', date_utc) as month, COUNT(*) as count
            FROM conversation_summaries
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """).fetchall()

        # Total meals
        total_meals = cur.execute("SELECT COUNT(*) as count FROM meals").fetchone()["count"]

        return jsonify({
            "total_memories": total_memories,
            "earliest_date": date_range["earliest"],
            "latest_date": date_range["latest"],
            "monthly_stats": [row_to_dict(row) for row in monthly_stats],
            "total_meals": total_meals
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/memory/<int:memory_id>")
def get_memory(memory_id: int) -> Response:
    """Get a single memory by ID."""
    conn = get_db()
    cur = conn.cursor()

    try:
        row = cur.execute("""
            SELECT * FROM conversation_summaries WHERE id = ?
        """, (memory_id,)).fetchone()

        if row:
            memory = row_to_dict(row)
            if memory.get("topics"):
                memory["topics_list"] = [t.strip() for t in memory["topics"].split(",") if t.strip()]
            else:
                memory["topics_list"] = []
            return jsonify({"memory": memory})
        else:
            return jsonify({"error": "Memory not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/memory/<int:memory_id>", methods=["DELETE"])
def delete_memory(memory_id: int) -> Response:
    """Delete a memory by ID."""
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("DELETE FROM conversation_summaries WHERE id = ?", (memory_id,))
        conn.commit()

        if cur.rowcount > 0:
            return jsonify({"success": True, "message": "Memory deleted"})
        else:
            return jsonify({"error": "Memory not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/meal/<int:meal_id>", methods=["DELETE"])
def delete_meal(meal_id: int) -> Response:
    """Delete a meal by ID."""
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        conn.commit()

        if cur.rowcount > 0:
            return jsonify({"success": True, "message": "Meal deleted"})
        else:
            return jsonify({"error": "Meal not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Graph Memory (v2) API
# ─────────────────────────────────────────────────────────────────────────────

def get_graph_store() -> GraphMemoryStore:
    """Get or create the graph memory store (shares the same DB)."""
    global _graph_store
    if _graph_store is None:
        _graph_store = GraphMemoryStore(_get_db_path())
    return _graph_store


@bp.route("/api/graph/nodes")
def graph_get_all_nodes() -> Response:
    """Get all nodes for the graph visualisation."""
    store = get_graph_store()
    try:
        root_id = request.args.get("root", "root")
        max_depth = min(int(request.args.get("max_depth", 10)), 20)
        data = store.get_graph_data(root_id, max_depth=max_depth)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/tree")
def graph_get_tree() -> Response:
    """Get the full tree structure for the sidebar."""
    store = get_graph_store()
    try:
        root_id = request.args.get("root", "root")
        max_depth = min(int(request.args.get("max_depth", 10)), 20)
        tree = store.get_subtree(root_id, max_depth=max_depth)
        return jsonify(tree)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/node/<node_id>")
def graph_get_node(node_id: str) -> Response:
    """Get a single node with its children and ancestors."""
    store = get_graph_store()
    try:
        node = store.get_node(node_id)
        if node is None:
            return jsonify({"error": "Node not found"}), 404

        store.touch_node(node_id)
        children = store.get_children(node_id)
        ancestors = store.get_ancestors(node_id)

        return jsonify({
            "node": node.to_dict(),
            "children": [c.to_dict() for c in children],
            "ancestors": [a.to_dict() for a in ancestors],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/node", methods=["POST"])
def graph_create_node() -> Response:
    """Create a new memory node."""
    store = get_graph_store()
    try:
        body = request.get_json()
        if not body or not body.get("name"):
            return jsonify({"error": "name is required"}), 400

        # Validate field types
        name = body["name"]
        description = body.get("description", "")
        data = body.get("data", "")
        parent_id = body.get("parent_id", "root")
        if not isinstance(name, str) or not isinstance(description, str) \
                or not isinstance(data, str) or not isinstance(parent_id, str):
            return jsonify({"error": "name, description, data, and parent_id must be strings"}), 400

        node = store.create_node(
            name=name,
            description=description,
            data=data,
            parent_id=parent_id,
        )
        return jsonify({"node": node.to_dict()}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/node/<node_id>", methods=["PUT"])
def graph_update_node(node_id: str) -> Response:
    """Update an existing memory node."""
    store = get_graph_store()
    try:
        body = request.get_json()
        if not body:
            return jsonify({"error": "Request body is required"}), 400

        kwargs = {}
        for field in ("name", "description", "data", "parent_id"):
            if field in body:
                if not isinstance(body[field], str):
                    return jsonify({"error": f"{field} must be a string"}), 400
                kwargs[field] = body[field]

        # Shares the lock every background flush (ambient digest, reply
        # turn) holds for its own read-transform-write span, so a manual
        # edit here can't land in the middle of one and get silently
        # overwritten by (or overwrite) an in-flight merge.
        with MEMORY_WRITE_LOCK:
            node = store.update_node(node_id, **kwargs)
        if node is None:
            return jsonify({"error": "Node not found or invalid parent"}), 404

        return jsonify({"node": node.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/node/<node_id>", methods=["DELETE"])
def graph_delete_node(node_id: str) -> Response:
    """Delete a memory node."""
    store = get_graph_store()
    try:
        if node_id == "root":
            return jsonify({"error": "Cannot delete root node"}), 400
        if node_id in FIXED_BRANCH_IDS:
            return jsonify({"error": "Cannot delete preset branch"}), 400

        deleted = store.delete_node(node_id)
        if deleted:
            return jsonify({"success": True})
        return jsonify({"error": "Node not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/presets")
def graph_presets() -> Response:
    """IDs of non-deletable preset nodes (root + FIXED_BRANCH_IDS).

    Single source of truth for the UI: avoids duplicating the branch list
    on the JS side, so adding a new fixed branch only requires editing
    ``FIXED_BRANCHES`` in graph.py.
    """
    return jsonify({"ids": ["root", *sorted(FIXED_BRANCH_IDS)]})


@bp.route("/api/graph/recent")
def graph_recent_nodes() -> Response:
    """Get recently accessed nodes."""
    store = get_graph_store()
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
        nodes = store.get_recent_nodes(limit)
        return jsonify({"nodes": [n.to_dict() for n in nodes]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/top")
def graph_top_nodes() -> Response:
    """Get most frequently accessed nodes."""
    store = get_graph_store()
    try:
        limit = min(int(request.args.get("limit", 15)), 50)
        nodes = store.get_top_nodes(limit)
        return jsonify({"nodes": [n.to_dict() for n in nodes]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/stats")
def graph_stats() -> Response:
    """Get graph memory statistics."""
    store = get_graph_store()
    try:
        return jsonify({
            "total_nodes": store.get_node_count(),
            "total_tokens": store.get_total_tokens(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/graph/import-diary", methods=["POST"])
def graph_import_diary() -> Response:
    """Import all diary conversation summaries into the graph memory system.

    Processes each summary through the extract → traverse → append → split
    pipeline. Returns a streaming response with progress updates so the UI
    can show real-time feedback.
    """
    from jarvis.config import load_settings
    from jarvis.memory.db import Database
    from jarvis.memory.graph_ops import update_graph_from_dialogue
    from jarvis.llm import resolve_model, Tier

    def generate():
        try:
            settings = load_settings()
            db_path = _get_db_path()
            db = Database(db_path, sqlite_vss_path=None)
            # Run the best-child picker on the small router-chain model so
            # historical import doesn't page in the big chat model for every
            # placement decision.
            picker_model = resolve_model(settings, Tier.FAST)

            summaries = db.get_all_conversation_summaries()
            total = len(summaries)

            if total == 0:
                yield json.dumps({"type": "complete", "message": "No diary entries found to import.", "processed": 0, "total": 0}) + "\n"
                return

            yield json.dumps({"type": "start", "total": total}) + "\n"

            store = get_graph_store()
            processed = 0
            total_facts = 0

            for row in summaries:
                summary_text = row["summary"]
                date_utc = row["date_utc"]
                error_msg = None

                try:
                    debug_log(f"graph import: processing {date_utc} ({len(summary_text)} chars)", "memory")
                    result = update_graph_from_dialogue(
                        store=store,
                        summary=summary_text,
                        cfg=settings,
                        chat_model=settings.llm_chat_model,
                        timeout_sec=settings.llm_chat_timeout_sec,
                        thinking=getattr(settings, 'llm_thinking_enabled', False),
                        date_utc=date_utc,
                        picker_model=picker_model,
                    )
                    facts_stored = len(result.stored)
                    total_facts += facts_stored
                except Exception as e:
                    debug_log(f"graph import: failed for {date_utc} — {e}", "memory")
                    facts_stored = 0
                    error_msg = str(e)

                processed += 1
                progress_msg = {
                    "type": "progress",
                    "processed": processed,
                    "total": total,
                    "date": date_utc,
                    "facts": facts_stored,
                }
                if error_msg:
                    progress_msg["error"] = error_msg
                yield json.dumps(progress_msg) + "\n"

            yield json.dumps({
                "type": "complete",
                "message": f"Imported {total_facts} facts from {total} diary entries.",
                "processed": processed,
                "total": total,
                "total_facts": total_facts,
            }) + "\n"

            db.close()

        except Exception as e:
            debug_log(f"graph import failed: {e}", "memory")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/graph/consolidate-all", methods=["POST"])
def graph_consolidate_all() -> Response:
    """Run the merge prompt's consolidation rules over every populated node.

    Migration path for nodes that accumulated contradictions before
    merge-on-write landed: under merge-on-write, a node only gets
    cleaned when a new related fact arrives, so backlog stays dirty
    until something nudges it. This endpoint nudges everything at
    once via `consolidate_all_populated_nodes`, streaming NDJSON
    progress so the UI can show per-node line-count deltas.
    """
    from jarvis.config import load_settings
    from jarvis.memory.graph_ops import (
        consolidate_all_populated_nodes,
        is_populated_node,
    )
    from jarvis.llm import resolve_model, Tier

    def generate():
        try:
            settings = load_settings()
            picker_model = resolve_model(settings, Tier.FAST)
            store = get_graph_store()

            # Count populated nodes upfront so the UI can render a
            # real progress bar. Reuses the shared predicate from
            # `graph_ops` so the count can never drift from the set
            # the generator actually walks. The double scan is
            # acceptable here — `get_all_nodes` is one cheap SQLite
            # read and the bar's accuracy is worth more than the saved
            # walk on the rarely-pressed maintenance op.
            total_nodes = sum(
                1 for n in store.get_all_nodes() if is_populated_node(n)
            )
            yield json.dumps({"type": "start", "total": total_nodes}) + "\n"

            total_before = 0
            total_after = 0
            node_count = 0
            # Stream per-node deltas as the generator yields them so
            # the UI gets real-time feedback on graphs with many
            # nodes — buffering the full sweep would defeat NDJSON.
            for name, before, after in consolidate_all_populated_nodes(
                store=store,
                cfg=settings,
                chat_model=settings.llm_chat_model,
                timeout_sec=20.0,
                thinking=getattr(settings, 'llm_thinking_enabled', False),
                picker_model=picker_model,
            ):
                node_count += 1
                total_before += before
                total_after += after
                yield json.dumps({
                    "type": "progress",
                    "node": name,
                    "before": before,
                    "after": after,
                    "delta": after - before,
                }) + "\n"

            yield json.dumps({
                "type": "complete",
                "nodes": node_count,
                "total_before": total_before,
                "total_after": total_after,
                "total_delta": total_after - total_before,
            }) + "\n"
        except Exception as e:
            debug_log(f"consolidate-all failed: {e}", "memory")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/diary/scrub-deflections", methods=["POST"])
def diary_scrub_deflections() -> Response:
    """Ask the chat model to remove deflection narration from every diary row.

    The summariser prompt forbids deflection narration at write time, but
    rows written before the prompt was tightened can still contain leaked
    phrasing. This endpoint walks every row and asks the configured chat
    model to rewrite it, dropping sentences that narrate the assistant's
    own failures while keeping everything else verbatim.

    Streams NDJSON progress so the UI can render per-row deltas. Crucially,
    the event payload contains *only* counts (char deltas, booleans, the
    date) — never raw summary text — so this endpoint cannot leak diary
    content to the UI.

    Requires the chat model to be running. Per-row rewrite failures are
    fail-open: the row is left untouched, the sweep continues.
    """
    from jarvis.config import load_settings
    from jarvis.memory.conversation import rewrite_all_diary_summaries
    from jarvis.memory.db import Database

    def generate():
        db = None
        try:
            settings = load_settings()
            db_path = _get_db_path()
            # Open with the configured VSS path so embedding refresh
            # actually targets the same vector store the rest of the app
            # reads from. Without this the bulk sweep would silently skip
            # re-embedding on installations that have VSS enabled.
            sqlite_vss_path = getattr(settings, "sqlite_vss_path", None)
            db = Database(db_path, sqlite_vss_path=sqlite_vss_path)

            total = len(db.get_all_conversation_summaries())
            yield json.dumps({"type": "start", "total": total}) + "\n"

            if total == 0:
                yield json.dumps({
                    "type": "complete",
                    "rows": 0,
                    "rows_rewritten": 0,
                    "rows_would_empty": 0,
                    "embeddings_refreshed": 0,
                }) + "\n"
                return

            rows_rewritten = 0
            rows_would_empty = 0
            rows_seen = 0
            embeddings_refreshed = 0

            for event in rewrite_all_diary_summaries(db, settings):
                rows_seen += 1
                if event.get("rewritten"):
                    rows_rewritten += 1
                if event.get("would_empty"):
                    rows_would_empty += 1
                if event.get("embedding_refreshed"):
                    embeddings_refreshed += 1
                yield json.dumps({
                    "type": "progress",
                    "processed": rows_seen,
                    "total": total,
                    **event,
                }) + "\n"

            yield json.dumps({
                "type": "complete",
                "rows": rows_seen,
                "rows_rewritten": rows_rewritten,
                "rows_would_empty": rows_would_empty,
                "embeddings_refreshed": embeddings_refreshed,
            }) + "\n"
        except Exception as e:
            debug_log(f"diary rewrite failed: {type(e).__name__}", "memory")
            # Surface only the class name to the streaming UI so a
            # corrupted row's content cannot leak via the exception
            # message.
            yield json.dumps({"type": "error", "message": type(e).__name__}) + "\n"
        finally:
            # The connection leaks if we close only on the success path —
            # a mid-iteration exception would orphan it until GC.
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/diary/optimise-topics", methods=["POST"])
def diary_optimise_topics() -> Response:
    """Normalise topic tags across every diary row via one LLM call.

    Collects all unique tags, asks the configured chat model to propose a
    normalised taxonomy (merging synonyms, splitting compound tags), then
    applies the mapping to every row whose topics change. Streams NDJSON
    progress so the UI shows per-row feedback in real time.

    Event payload contains only counts and the date — never raw tag strings
    — so this endpoint cannot leak diary content to the streaming UI.
    """
    from jarvis.config import load_settings
    from jarvis.memory.conversation import optimise_diary_topics
    from jarvis.memory.db import Database

    def generate():
        db = None
        try:
            settings = load_settings()
            db_path = _get_db_path()
            sqlite_vss_path = getattr(settings, "sqlite_vss_path", None)
            db = Database(db_path, sqlite_vss_path=sqlite_vss_path)

            total = len(db.get_all_conversation_summaries())
            yield json.dumps({"type": "start", "total": total}) + "\n"

            if total == 0:
                yield json.dumps({
                    "type": "complete",
                    "rows": 0,
                    "rows_changed": 0,
                    "topics_merged": 0,
                    "topics_expanded": 0,
                }) + "\n"
                return

            rows_changed = 0
            rows_seen = 0
            topics_merged = 0
            topics_expanded = 0

            for event in optimise_diary_topics(db, settings):
                rows_seen += 1
                if event.get("topics_changed"):
                    rows_changed += 1
                    old_n = event.get("old_topic_count", 0)
                    new_n = event.get("new_topic_count", 0)
                    if new_n < old_n:
                        topics_merged += old_n - new_n
                    elif new_n > old_n:
                        topics_expanded += new_n - old_n
                yield json.dumps({
                    "type": "progress",
                    "processed": rows_seen,
                    "total": total,
                    **event,
                }) + "\n"

            yield json.dumps({
                "type": "complete",
                "rows": rows_seen,
                "rows_changed": rows_changed,
                "topics_merged": topics_merged,
                "topics_expanded": topics_expanded,
            }) + "\n"
        except Exception as e:
            debug_log(f"diary topic optimise failed: {type(e).__name__}", "memory")
            yield json.dumps({"type": "error", "message": type(e).__name__}) + "\n"
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

