"""Behavioural tests for the explicit school-note graph import."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis.memory.graph import GraphMemoryStore
from jarvis.memory.school_import import import_school_notes
from jarvis.memory.vault.index import VaultIndex


pytestmark = pytest.mark.unit


def _cfg(vault):
    return SimpleNamespace(
        obsidian_vault_path=str(vault),
        obsidian_memory_folder="Jarvis",
        obsidian_index_max_file_kb=512,
        llm_provider="ollama",
        llm_base_url="http://localhost:11434",
        llm_api_key="",
        llm_chat_model="test-model",
        ollama_chat_model="test-model",
        fast_model="test-model",
    )


def _write_school_note(vault, relative_path, content):
    path = vault / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_import_is_bounded_to_school_sources_and_idempotent(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_school_note(
        vault,
        "05 - Schule/Biology.md",
        "# Biology\nThe biology exam is on 2 October.",
    )
    _write_school_note(
        vault,
        "Projects/Private.md",
        "# Private\nA fact that must not be imported.",
    )
    store = GraphMemoryStore(str(tmp_path / "graph.db"))
    index = VaultIndex(vault, memory_folder="Jarvis")
    first_response = (
        '[{"branch": "USER", "fact": "The biology exam is on 2 October"}]'
    )
    paraphrased_response = (
        '[{"branch": "SCHOOL", "fact": '
        '"Felix has his biology examination on 2 October"}]'
    )

    try:
        with patch.object(
            index,
            "_candidate_files",
            side_effect=AssertionError("bounded read must not scan the whole vault"),
        ), patch(
            "jarvis.memory.graph_ops.call_llm_direct",
            side_effect=[first_response, paraphrased_response],
        ):
            first = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=index,
                max_notes=10,
            )
            second = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=index,
                max_notes=10,
            )

        assert first.notes_processed == 1
        assert first.facts_stored == 1
        assert second.notes_processed == 1
        assert second.facts_stored == 0
        assert second.notes_unchanged == 1
        assert store.get_node("school").data.count("biology exam") == 1
        assert "examination" not in store.get_node("school").data
        assert store.get_node("user").data == ""
        assert "Private" not in store.get_node("school").data
    finally:
        store.close()


def test_import_limits_number_of_notes_per_run(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_school_note(vault, "05 - Schule/A.md", "First school note")
    _write_school_note(vault, "05 - Schule/B.md", "Second school note")
    store = GraphMemoryStore(str(tmp_path / "graph.db"))

    try:
        with patch(
            "jarvis.memory.graph_ops.call_llm_direct",
            return_value='[{"branch": "SCHOOL", "fact": "One school fact"}]',
        ):
            result = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=VaultIndex(vault),
                max_notes=1,
            )

        assert result.notes_processed == 1
    finally:
        store.close()


def test_failed_extraction_is_retried_instead_of_marked_current(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_school_note(
        vault,
        "05 - Schule/Chemistry.md",
        "The chemistry homework is due Friday.",
    )
    store = GraphMemoryStore(str(tmp_path / "graph.db"))

    try:
        with patch(
            "jarvis.memory.graph_ops.call_llm_direct",
            return_value=None,
        ) as model_call:
            first = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=VaultIndex(vault),
            )
            second = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=VaultIndex(vault),
            )

        assert first.errors == 1
        assert second.errors == 1
        assert second.notes_unchanged == 0
        assert model_call.call_count == 2
    finally:
        store.close()


def test_valid_empty_extraction_is_marked_current(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_school_note(vault, "05 - Schule/Empty.md", "No durable school facts.")
    store = GraphMemoryStore(str(tmp_path / "graph.db"))

    try:
        with patch(
            "jarvis.memory.graph_ops.call_llm_direct",
            return_value="[]",
        ) as model_call:
            first = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=VaultIndex(vault),
            )
            second = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=VaultIndex(vault),
            )

        assert first.errors == 0
        assert second.notes_unchanged == 1
        assert model_call.call_count == 1
    finally:
        store.close()


def test_hostile_note_title_cannot_cause_a_vault_write(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = _write_school_note(
        vault,
        "05 - Schule/Hostile.md",
        "# ../../Outside\nThe chemistry homework is due Friday.",
    )
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*") if path.is_file()
    }
    store = GraphMemoryStore(str(tmp_path / "graph.db"))

    try:
        with patch(
            "jarvis.memory.graph_ops.call_llm_direct",
            return_value=(
                '[{"branch": "SCHOOL", "fact": '
                '"The chemistry homework is due Friday"}]'
            ),
        ):
            result = import_school_notes(
                store=store,
                cfg=_cfg(vault),
                index=VaultIndex(vault),
            )

        after = {
            path.relative_to(vault).as_posix(): path.read_bytes()
            for path in vault.rglob("*") if path.is_file()
        }
        assert result.facts_stored == 1
        assert after == before
        assert note.read_text(encoding="utf-8").startswith("# ../../Outside")
        assert not (tmp_path / "Outside.md").exists()
    finally:
        store.close()
