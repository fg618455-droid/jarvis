from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.memory.graph import MemoryNode
from jarvis.memory.vault.mirror import apply_sync, plan_sync
from jarvis.memory.vault.render import END_MARKER, filename_for_node


pytestmark = pytest.mark.unit


class Store:
    def __init__(self, nodes):
        self.nodes = {node.id: node for node in nodes}

    def get_all_nodes(self):
        return list(self.nodes.values())

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def get_children(self, node_id):
        return [node for node in self.nodes.values() if node.parent_id == node_id]


def _node(node_id, name, parent_id, data="fact", description="description"):
    return MemoryNode(
        id=node_id,
        name=name,
        description=description,
        data=data,
        parent_id=parent_id,
        created_at="2026-08-11T10:00:00+02:00",
        updated_at="2026-08-11T14:03:00+02:00",
        data_token_count=max(1, len(data) // 4),
    )


def _store():
    return Store([
        _node("root", "Root", None, data=""),
        _node("user", "User", "root", data=""),
        _node("directives", "Directives", "root", data=""),
        _node("world", "World", "root", data=""),
        _node("11111111-1111-1111-1111-111111111111", "Food", "user"),
        _node("22222222-2222-2222-2222-222222222222", "Restaurants", "11111111-1111-1111-1111-111111111111"),
    ])


def _cfg(vault: Path, mode="on"):
    return SimpleNamespace(
        obsidian_vault_path=str(vault),
        obsidian_memory_folder="Jarvis",
        obsidian_write_mode=mode,
    )


def _snapshot(root: Path):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _apply_initial(store, vault):
    plan = plan_sync(store, _cfg(vault))
    apply_sync(plan, _cfg(vault))
    return plan


def test_dry_run_computes_plan_and_leaves_vault_byte_identical(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    private = vault / "private.md"
    private.write_text("irreplaceable", encoding="utf-8")
    before = _snapshot(vault)

    plan = plan_sync(_store(), _cfg(vault, "dry_run"))

    assert any(change.action == "create" for change in plan.changes)
    assert _snapshot(vault) == before
    assert apply_sync(plan, _cfg(vault, "dry_run")) == 0
    assert _snapshot(vault) == before


@pytest.mark.parametrize("mode", ["off", "dry_run"])
def test_write_modes_other_than_on_refuse_all_writes(tmp_path, mode):
    vault = tmp_path / "vault"
    vault.mkdir()
    plan = plan_sync(_store(), _cfg(vault, "dry_run"))
    before = _snapshot(vault)

    assert apply_sync(plan, _cfg(vault, mode)) == 0
    assert _snapshot(vault) == before


def test_unchanged_nodes_are_skipped_without_rewrite(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = _store()
    _apply_initial(store, vault)
    mtimes = {path: path.stat().st_mtime_ns for path in (vault / "Jarvis").glob("*.md")}

    plan = plan_sync(store, _cfg(vault))

    assert {change.action for change in plan.changes} == {"skip"}
    apply_sync(plan, _cfg(vault))
    assert {path: path.stat().st_mtime_ns for path in mtimes} == mtimes


def test_unmanaged_collision_is_refused_and_never_overwritten(tmp_path):
    vault = tmp_path / "vault"
    memory = vault / "Jarvis"
    memory.mkdir(parents=True)
    store = _store()
    food = store.get_node("11111111-1111-1111-1111-111111111111")
    collision = memory / filename_for_node(food, "User")
    collision.write_text("my private note", encoding="utf-8")

    plan = plan_sync(store, _cfg(vault))
    apply_sync(plan, _cfg(vault))

    matching = [c for c in plan.changes if c.node_id == food.id]
    assert matching and matching[0].action == "refuse"
    assert collision.read_text(encoding="utf-8") == "my private note"


def test_sync_never_writes_elsewhere_in_vault(tmp_path):
    vault = tmp_path / "vault"
    notes = vault / "Projects" / "private.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("private", encoding="utf-8")
    before = _snapshot(vault)

    _apply_initial(_store(), vault)

    after = _snapshot(vault)
    assert after["Projects/private.md"] == before["Projects/private.md"]
    assert all(path.startswith("Jarvis/") or path == "Projects/private.md" for path in after)


def test_orphan_sweep_deletes_only_owned_files(tmp_path):
    vault = tmp_path / "vault"
    memory = vault / "Jarvis"
    memory.mkdir(parents=True)
    managed = memory / "orphan.md"
    managed.write_text(
        "---\njarvis_managed: true\njarvis_node_id: gone\n---\n" + END_MARKER,
        encoding="utf-8",
    )
    private = memory / "private.md"
    private.write_text("jarvis_node_id: gone-too", encoding="utf-8")

    plan = plan_sync(_store(), _cfg(vault))
    apply_sync(plan, _cfg(vault))

    assert any(c.action == "delete" and c.path == managed for c in plan.changes)
    assert not managed.exists()
    assert private.read_text(encoding="utf-8") == "jarvis_node_id: gone-too"


def test_mutation_pass_never_emits_delete(tmp_path):
    vault = tmp_path / "vault"
    memory = vault / "Jarvis"
    memory.mkdir(parents=True)
    orphan = memory / "orphan.md"
    orphan.write_text(
        "---\njarvis_managed: true\njarvis_node_id: gone\n---\n" + END_MARKER,
        encoding="utf-8",
    )

    plan = plan_sync(_store(), _cfg(vault), node_ids={"user"}, full_sweep=False)

    assert all(change.action != "delete" for change in plan.changes)
    assert orphan.exists()


def test_protected_tail_survives_complete_machine_rewrite(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = _store()
    _apply_initial(store, vault)
    food = store.get_node("11111111-1111-1111-1111-111111111111")
    path = vault / "Jarvis" / filename_for_node(food, "User")
    tail = "\n\n## My annotations\nKeep [[this]] verbatim.\n"
    path.write_text(path.read_text(encoding="utf-8") + tail, encoding="utf-8")
    store.nodes[food.id] = replace(food, name="Meals", description="different", data="entirely new")

    apply_sync(plan_sync(store, _cfg(vault)), _cfg(vault))

    rewritten = vault / "Jarvis" / filename_for_node(store.nodes[food.id], "User")
    assert rewritten.read_text(encoding="utf-8").endswith(tail)
    assert "entirely new" in rewritten.read_text(encoding="utf-8")


def test_markerless_owned_file_gets_marker_and_replaced_machine_section(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = _store()
    _apply_initial(store, vault)
    food = store.get_node("11111111-1111-1111-1111-111111111111")
    path = vault / "Jarvis" / filename_for_node(food, "User")
    markerless = path.read_text(encoding="utf-8").replace(END_MARKER, "stale machine text")
    path.write_text(markerless, encoding="utf-8")
    store.nodes[food.id] = replace(food, data="fresh fact")

    apply_sync(plan_sync(store, _cfg(vault)), _cfg(vault))

    content = path.read_text(encoding="utf-8")
    assert END_MARKER in content
    assert "fresh fact" in content
    assert "stale machine text" not in content


def test_rename_includes_parent_and_child_link_fixups(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = _store()
    _apply_initial(store, vault)
    target_id = "11111111-1111-1111-1111-111111111111"
    old = store.get_node(target_id)
    store.nodes[target_id] = replace(old, name="Meals")

    plan = plan_sync(store, _cfg(vault), node_ids={target_id}, full_sweep=False)
    actions = {(change.node_id, change.action) for change in plan.changes}

    assert (target_id, "rename") in actions
    assert ("user", "update") in actions
    assert ("22222222-2222-2222-2222-222222222222", "update") in actions
    apply_sync(plan, _cfg(vault))
    assert not (vault / "Jarvis" / filename_for_node(old, "User")).exists()
    assert (vault / "Jarvis" / filename_for_node(store.nodes[target_id], "User")).exists()


def test_missing_vault_degrades_to_empty_plan_without_raising(tmp_path):
    plan = plan_sync(_store(), _cfg(tmp_path / "missing"))
    assert plan.changes == []
