"""Behavioural tests for carried memory provenance."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from jarvis.memory.provenance import (
    MemoryProvenance,
    RetrievedSnippet,
)


def test_diary_search_attaches_the_entry_date_as_provenance():
    from jarvis.memory.conversation import search_conversation_memory_by_keywords

    class _Database:
        def search_hybrid(self, query, embedding, top_k):
            del query, embedding, top_k
            return [{
                "text": "[2026-08-24] The user chose the blue design.",
                "score": 0.8,
            }]

    cfg = SimpleNamespace(embedding_model=None)
    snippets = search_conversation_memory_by_keywords(
        db=_Database(), cfg=cfg, keywords=["blue", "design"],
    )

    assert snippets[0].provenance == MemoryProvenance.diary("2026-08-24")


def test_graph_snippet_carries_node_id_and_fixed_branch():
    from jarvis.memory.provenance import graph_snippet

    snippet = graph_snippet(
        "The user prefers trains.",
        node_id="5dc9a3c1-2f2d-4f93-8d29-269e02aa4f78",
        branch="user",
    )

    assert snippet.provenance == MemoryProvenance.graph(
        "5dc9a3c1-2f2d-4f93-8d29-269e02aa4f78", "user",
    )


def test_school_is_a_valid_fixed_branch_for_graph_provenance():
    provenance = MemoryProvenance.graph("school-node", "school")

    assert provenance.public_dict() == {
        "kind": "graph",
        "node_id": "school-node",
        "branch": "school",
    }


def test_vault_search_attaches_the_vault_relative_path(tmp_path):
    from jarvis.memory.vault.index import VaultIndex

    note = tmp_path / "Projects" / "Alpha.md"
    note.parent.mkdir()
    note.write_text("# Alpha\nlaunch checklist", encoding="utf-8")

    hit = VaultIndex(tmp_path).search("alpha checklist")[0]

    assert hit.provenance == MemoryProvenance.vault("Projects/Alpha.md")


def test_remio_search_attaches_the_note_title():
    from subprocess import CompletedProcess
    from jarvis.memory.remio import RemioAdapter

    def run(command, **kwargs):
        del kwargs
        if command[1] == "search_notes":
            body = {"ok": True, "data": {"results": [{
                "noteId": "note-1", "title": "Project Alpha",
            }]}}
        else:
            body = {"ok": True, "data": {"content": "Launch on Friday."}}
        return CompletedProcess(command, 0, json.dumps(body), "")

    snippet = RemioAdapter(run=run).search("alpha")[0]

    assert snippet.provenance == MemoryProvenance.remio("Project Alpha")


def test_parallel_merge_keeps_each_source_attached_to_its_own_snippet():
    from jarvis.memory.retrieval import retrieve_parallel

    expected = {
        "diary fact": MemoryProvenance.diary("2026-08-20"),
        "graph fact": MemoryProvenance.graph("node-7", "world"),
        "vault fact": MemoryProvenance.vault("Reference/Fact.md"),
        "remio fact": MemoryProvenance.remio("Fact note"),
    }

    def source(text, provenance, delay):
        def retrieve():
            time.sleep(delay)
            return [RetrievedSnippet(text, provenance)]
        return retrieve

    merged = retrieve_parallel(
        [source(text, provenance, delay / 100) for delay, (text, provenance) in enumerate(expected.items())],
        timeout_sec=1.0,
    )

    assert {snippet.text: snippet.provenance for snippet in merged} == expected


def test_provenance_tool_returns_raw_structured_records():
    from jarvis.tools.builtin.memory_provenance import MemoryProvenanceTool

    snippets = [RetrievedSnippet(
        "The user chose the blue design.",
        MemoryProvenance.diary("2026-08-24"),
    )]
    result = MemoryProvenanceTool().run(
        {}, SimpleNamespace(memory_snippets=snippets),
    )

    assert result.success
    payload = json.loads(result.reply_text)
    assert payload == {
        "status": "recorded",
        "records": [{
            "snippet": "The user chose the blue design.",
            "provenance": {"kind": "diary", "date": "2026-08-24"},
        }],
        "unrecorded_snippet_count": 0,
        "source_claim_policy": "cite_matching_records_only",
        "missing_origin_policy": "report_not_recorded_without_inference",
    }
    assert "I know" not in result.reply_text


def test_provenance_tool_is_registered_with_semantic_routing_guidance():
    from jarvis.tools.registry import BUILTIN_TOOLS

    tool = BUILTIN_TOOLS["memoryProvenance"]

    assert "in any language" in tool.description
    assert "where a remembered personal fact came" in tool.description
    assert "Do not use merely to recall" in tool.description


def test_provenance_tool_reports_unrecorded_instead_of_guessing():
    from jarvis.tools.builtin.memory_provenance import MemoryProvenanceTool

    result = MemoryProvenanceTool().run(
        {}, SimpleNamespace(memory_snippets=[RetrievedSnippet("The user likes tea.")]),
    )

    payload = json.loads(result.reply_text)
    assert payload["status"] == "not_recorded"
    assert payload["records"] == []
    assert payload["unrecorded_snippet_count"] == 1


def test_mixed_sourced_and_unrecorded_context_is_explicitly_partial():
    from jarvis.tools.builtin.memory_provenance import MemoryProvenanceTool

    result = MemoryProvenanceTool().run(
        {},
        SimpleNamespace(memory_snippets=[
            RetrievedSnippet(
                "A sourced fact.",
                MemoryProvenance.diary("2026-08-24"),
            ),
            RetrievedSnippet("An unsourced warm-profile fact."),
        ]),
    )

    payload = json.loads(result.reply_text)
    assert payload["status"] == "partial"
    assert len(payload["records"]) == 1
    assert payload["unrecorded_snippet_count"] == 1


def test_hostile_vault_identifier_cannot_render_as_an_external_path():
    from jarvis.tools.builtin.memory_provenance import MemoryProvenanceTool

    result = MemoryProvenanceTool().run(
        {},
        SimpleNamespace(memory_snippets=[RetrievedSnippet(
            "hostile",
            MemoryProvenance.vault("../../outside/secrets.md"),
        )]),
    )

    payload = json.loads(result.reply_text)
    assert payload["records"][0]["provenance"] == {
        "kind": "vault",
        "path_status": "invalid",
    }
    assert "outside" not in result.reply_text
    assert ".." not in result.reply_text


def test_disclosed_vault_path_is_removed_before_hot_window_storage():
    from jarvis.memory.provenance import redact_vault_paths

    snippets = [RetrievedSnippet(
        "Launch checklist.",
        MemoryProvenance.vault("Projects/Alpha.md"),
    )]

    scrubbed = redact_vault_paths(
        "It came from Projects/Alpha.md.", snippets,
    )

    assert scrubbed == "It came from [vault path disclosed on request]."


def test_provenance_tool_messages_are_excluded_from_later_tool_carryover():
    from jarvis.reply.engine import _without_memory_provenance_carryover

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "source-call",
                "function": {"name": "memoryProvenance", "arguments": {}},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "source-call",
            "content": '{"path":"Private/Health.md"}',
        },
        {
            "role": "user",
            "tool_name": "memoryProvenance",
            "content": '{"path":"Private/Health.md"}',
        },
        {
            "role": "tool",
            "tool_call_id": "other-call",
            "content": "safe result",
        },
    ]

    retained = _without_memory_provenance_carryover(messages)

    assert retained == [{
        "role": "tool",
        "tool_call_id": "other-call",
        "content": "safe result",
    }]
