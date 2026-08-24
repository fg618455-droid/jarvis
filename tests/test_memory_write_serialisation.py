"""Diary and graph-node mutations must not interleave across threads.

Each mutation reads existing state, sends it through a slow LLM round trip,
and writes the result back. Several independent threads can reach these same
entry points concurrently — the ambient digest worker, the reply-turn
pipeline, and the control centre's manual editing endpoints all mutate the
same diary rows and graph nodes. Without a lock spanning the full
read-transform-write sequence, two threads racing the same diary day or the
same graph node can each read the pre-write value, compute independently,
and the second write silently discards the first (a lost update). These
tests force two callers through the same critical section at once and
assert they never overlap.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        llm_provider="ollama",
        llm_base_url="http://localhost:11434",
        llm_chat_model="test",
        embedding_model="",
        ollama_base_url="http://localhost:11434",
        ollama_chat_model="test",
        ollama_embed_model="test",
    )


class _OverlapDetector:
    """Records whether two callers were ever inside a critical section
    at the same time."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.overlapped = False

    def enter(self):
        with self._lock:
            self.active += 1
            if self.active > 1:
                self.overlapped = True

    def exit(self):
        with self._lock:
            self.active -= 1


@pytest.mark.integration
class TestDiarySummaryWriteSerialisation:
    def test_two_threads_updating_the_same_day_do_not_interleave(self, db):
        from jarvis.memory.conversation import update_daily_conversation_summary

        detector = _OverlapDetector()

        def fake_generate(chunks, previous_summary, cfg, **kwargs):
            detector.enter()
            try:
                time.sleep(0.05)
                return f"summary incorporating {chunks[0]}", "topic"
            finally:
                detector.exit()

        with patch(
            "jarvis.memory.conversation.generate_conversation_summary",
            side_effect=fake_generate,
        ):
            threads = [
                threading.Thread(
                    target=update_daily_conversation_summary,
                    kwargs=dict(
                        db=db, new_chunks=[f"chunk-{i}"], cfg=_cfg(),
                        date_utc="2026-01-01",
                    ),
                )
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert detector.overlapped is False


@pytest.mark.integration
class TestGraphNodeMergeWriteSerialisation:
    def test_two_flushes_merging_into_the_same_node_do_not_interleave(self, tmp_path):
        from jarvis.memory.graph import GraphMemoryStore
        from jarvis.memory import graph_ops

        store = GraphMemoryStore(str(tmp_path / "graph.db"))
        node = store.create_node(
            name="Health",
            description="Health facts",
            data="Runs 3 times a week.",
            parent_id="root",
        )

        detector = _OverlapDetector()

        def fake_extract(summary, cfg, chat_model, timeout_sec=30.0,
                          thinking=False, date_utc=None):
            return [("user", summary)]

        def fake_find_best_node(*, store, fragment, cfg, chat_model,
                                 timeout_sec=15.0, thinking=False,
                                 picker_model=None, branch_root_id=None):
            return node.id

        def fake_merge(*, store, node_id, new_facts, cfg, chat_model,
                        timeout_sec=20.0, thinking=False, picker_model=None,
                        node=None):
            detector.enter()
            try:
                time.sleep(0.05)
                current = store.get_node(node_id)
                combined = current.data + "\n" + "\n".join(new_facts)
                store.update_node(node_id, data=combined)
                return graph_ops.MergeResult(
                    success=True, incorporated_indices=list(range(len(new_facts))),
                )
            finally:
                detector.exit()

        with patch.object(graph_ops, "extract_graph_memories", side_effect=fake_extract), \
             patch.object(graph_ops, "find_best_node", side_effect=fake_find_best_node), \
             patch.object(graph_ops, "merge_node_data", side_effect=fake_merge):
            threads = [
                threading.Thread(
                    target=graph_ops.update_graph_from_dialogue,
                    kwargs=dict(
                        store=store, summary=f"fact-{i}", cfg=_cfg(),
                        chat_model="test",
                    ),
                )
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert detector.overlapped is False
