"""Planning, atomic application, and debounced graph-to-vault mirroring."""

from __future__ import annotations

import difflib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ...debug import debug_log
from ..graph import FIXED_BRANCH_IDS, MemoryNode
from .guard import VaultWriteError, resolve_managed_path
from .render import (
    filename_for_node,
    is_managed_markdown,
    managed_node_id,
    render_node,
    split_protected_region,
)


_ACTION_ORDER = {"rename": 0, "create": 1, "update": 2, "skip": 3, "refuse": 4, "delete": 5}


@dataclass(frozen=True)
class PlannedChange:
    action: str
    node_id: str | None
    path: Path
    reason: str
    diff: str = ""
    source_path: Path | None = field(default=None, repr=False, compare=False)
    rendered: str | None = field(default=None, repr=False, compare=False)


@dataclass
class SyncPlan:
    changes: list[PlannedChange] = field(default_factory=list)

    def actionable(self) -> list[PlannedChange]:
        return [change for change in self.changes if change.action not in {"skip", "refuse"}]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        debug_log(f"vault note unreadable ({path.name}): {exc}", "vault")
        return None


def _branch_for(node: MemoryNode, nodes: dict[str, MemoryNode]) -> tuple[str, str]:
    current = node
    visited: set[str] = set()
    while current.id not in visited:
        visited.add(current.id)
        if current.id in FIXED_BRANCH_IDS:
            return current.id, current.name
        parent = nodes.get(current.parent_id or "")
        if parent is None or parent.id == "root":
            return current.id, current.name
        current = parent
    return node.id, node.name


def _related_ids(selected: set[str], nodes: dict[str, MemoryNode]) -> set[str]:
    expanded = set(selected)
    for node_id in list(selected):
        node = nodes.get(node_id)
        if node is None:
            continue
        if node.parent_id and node.parent_id != "root":
            expanded.add(node.parent_id)
        expanded.update(child.id for child in nodes.values() if child.parent_id == node_id)
    return expanded


def _line_diff(old: str, new: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=name,
            tofile=name,
        )
    )


def plan_sync(
    store,
    cfg,
    *,
    node_ids: Iterable[str] | None = None,
    full_sweep: bool = True,
) -> SyncPlan:
    """Compare graph state with the managed folder without changing disk."""
    vault_value = getattr(cfg, "obsidian_vault_path", None)
    folder_value = getattr(cfg, "obsidian_memory_folder", None)
    if not vault_value or not folder_value:
        return SyncPlan()
    vault = Path(vault_value).expanduser()
    try:
        vault = vault.resolve(strict=True)
        if not vault.is_dir():
            return SyncPlan()
        # Validate the folder boundary even when no graph nodes exist.
        resolve_managed_path(vault, folder_value, "boundary-check.md")
    except (OSError, VaultWriteError) as exc:
        debug_log(f"vault sync planning skipped: {exc}", "vault")
        return SyncPlan()

    try:
        all_nodes = list(store.get_all_nodes())
    except Exception as exc:
        debug_log(f"vault graph read failed: {exc}", "vault")
        return SyncPlan()
    nodes = {node.id: node for node in all_nodes}
    mirrored_nodes = {node_id: node for node_id, node in nodes.items() if node_id != "root"}
    if node_ids is None:
        selected_ids = set(mirrored_nodes)
    else:
        selected_ids = _related_ids(set(node_ids), nodes) & set(mirrored_nodes)

    memory = (vault / Path(folder_value)).resolve(strict=False)
    existing_contents: dict[Path, str] = {}
    existing_by_id: dict[str, Path] = {}
    if memory.is_dir():
        try:
            candidates = list(memory.glob("*.md"))
        except OSError as exc:
            debug_log(f"vault managed folder unreadable: {exc}", "vault")
            return SyncPlan()
        for path in candidates:
            content = _read_text(path)
            if content is None:
                continue
            existing_contents[path.resolve(strict=False)] = content
            node_id = managed_node_id(content)
            if node_id and node_id not in existing_by_id:
                existing_by_id[node_id] = path.resolve(strict=False)

    children_by_parent: dict[str, list[MemoryNode]] = {}
    for candidate in mirrored_nodes.values():
        if candidate.parent_id:
            children_by_parent.setdefault(candidate.parent_id, []).append(candidate)

    changes: list[PlannedChange] = []
    for node_id in sorted(selected_ids):
        node = mirrored_nodes[node_id]
        branch_id, branch_label = _branch_for(node, nodes)
        try:
            target = resolve_managed_path(
                vault,
                folder_value,
                filename_for_node(node, branch_label),
            )
        except VaultWriteError as exc:
            changes.append(PlannedChange("refuse", node.id, memory / "invalid.md", str(exc)))
            continue

        old_path = existing_by_id.get(node.id)
        target_content = existing_contents.get(target)
        if target_content is not None and not is_managed_markdown(target_content):
            changes.append(
                PlannedChange("refuse", node.id, target, "an unmanaged file occupies the generated filename")
            )
            continue
        if target_content is not None and managed_node_id(target_content) != node.id:
            changes.append(
                PlannedChange("refuse", node.id, target, "a different managed node occupies the generated filename")
            )
            continue

        source_content = existing_contents.get(old_path) if old_path else None
        if source_content is None:
            source_content = target_content
        protected_tail: str | None = None
        if source_content is not None:
            _, protected_tail = split_protected_region(source_content)
        parent = nodes.get(node.parent_id or "")
        rendered = render_node(
            node,
            branch_id,
            branch_label,
            parent=parent,
            children=sorted(children_by_parent.get(node.id, []), key=lambda child: child.name.casefold()),
            protected_tail=protected_tail,
        )

        if old_path is not None and old_path != target:
            changes.append(
                PlannedChange(
                    "rename",
                    node.id,
                    target,
                    f"node filename changed from {old_path.name}",
                    _line_diff(source_content or "", rendered, target.name),
                    source_path=old_path,
                    rendered=rendered,
                )
            )
        elif target_content is None:
            changes.append(
                PlannedChange("create", node.id, target, "node is not mirrored", rendered=rendered)
            )
        elif target_content != rendered:
            changes.append(
                PlannedChange(
                    "update",
                    node.id,
                    target,
                    "rendered content changed",
                    _line_diff(target_content, rendered, target.name),
                    rendered=rendered,
                )
            )
        else:
            changes.append(PlannedChange("skip", node.id, target, "rendered content is unchanged"))

    if full_sweep:
        valid_ids = set(mirrored_nodes)
        for node_id, path in existing_by_id.items():
            if node_id not in valid_ids:
                changes.append(PlannedChange("delete", node_id, path, "managed node is absent from the graph"))

    changes.sort(key=lambda change: (_ACTION_ORDER.get(change.action, 99), str(change.path)))
    debug_log(
        f"vault sync planned {sum(c.action not in {'skip'} for c in changes)} changes",
        "vault",
    )
    return SyncPlan(changes)


def _validated_target(change: PlannedChange, cfg) -> Path:
    target = resolve_managed_path(
        getattr(cfg, "obsidian_vault_path"),
        getattr(cfg, "obsidian_memory_folder"),
        change.path.name,
    )
    if target != change.path.resolve(strict=False):
        raise VaultWriteError("Planned target changed after path validation")
    return target


def _atomic_write(target: Path, content: str) -> None:
    temp = target.with_name(target.name + ".tmp")
    if temp.resolve(strict=False).parent != target.parent:
        raise VaultWriteError("Temporary file resolves outside the managed folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        created = False
    finally:
        if created:
            try:
                temp.unlink()
            except OSError:
                pass


def apply_sync(plan: SyncPlan, cfg) -> int:
    """Apply a plan only under explicit ``on`` mode, preserving ownership."""
    if getattr(cfg, "obsidian_write_mode", "dry_run") != "on":
        debug_log("vault writes refused: write mode is not on", "vault")
        return 0
    applied = 0
    for change in plan.changes:
        if change.action in {"skip", "refuse"}:
            continue
        try:
            target = _validated_target(change, cfg)
            if change.action == "create":
                if target.exists():
                    raise VaultWriteError("Create target appeared after planning")
                _atomic_write(target, change.rendered or "")
            elif change.action == "update":
                existing = _read_text(target)
                if existing is None or not is_managed_markdown(existing):
                    raise VaultWriteError("Update target is not an owned note")
                _atomic_write(target, change.rendered or "")
            elif change.action == "rename":
                if change.source_path is None:
                    raise VaultWriteError("Rename has no source")
                source = resolve_managed_path(
                    getattr(cfg, "obsidian_vault_path"),
                    getattr(cfg, "obsidian_memory_folder"),
                    change.source_path.name,
                )
                source_content = _read_text(source)
                if source_content is None or not is_managed_markdown(source_content):
                    raise VaultWriteError("Rename source is not an owned note")
                if target.exists() and target != source:
                    raise VaultWriteError("Rename target appeared after planning")
                os.replace(source, target)
                _atomic_write(target, change.rendered or "")
            elif change.action == "delete":
                existing = _read_text(target)
                if existing is None or not is_managed_markdown(existing):
                    raise VaultWriteError("Delete target is not an owned note")
                target.unlink()
            else:
                continue
            applied += 1
            debug_log(f"vault {change.action}: {target.name}", "vault")
        except (OSError, VaultWriteError) as exc:
            debug_log(f"vault {change.action} refused for {change.path.name}: {exc}", "vault")
    return applied


def format_sync_plan(plan: SyncPlan) -> str:
    visible = [change for change in plan.changes if change.action != "skip"]
    lines = [f"  🗂️ Vault mirror plan: {len(visible)} change(s)"]
    icons = {
        "create": "➕",
        "update": "✏️",
        "rename": "🔄",
        "delete": "🗑️",
        "refuse": "⛔",
    }
    for change in visible:
        lines.append(f"     {icons.get(change.action, 'ℹ️')} {change.action}: {change.path.name}")
        if change.diff:
            for line in change.diff.splitlines():
                lines.append(f"        · {line}")
    return "\n".join(lines)


def print_sync_plan(plan: SyncPlan) -> None:
    report = format_sync_plan(plan)
    print(report, flush=True)
    debug_log(report.replace("\n", " | "), "vault")


class VaultMirrorWorker:
    """Single debounced worker that turns graph mutation IDs into sync passes."""

    def __init__(self, store, cfg, debounce_seconds: float = 3.0):
        self.store = store
        self.cfg = cfg
        self.debounce_seconds = debounce_seconds
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._relation_cache: dict[str, set[str]] = {}

    def _refresh_relation_cache(self) -> None:
        try:
            nodes = list(self.store.get_all_nodes())
        except Exception as exc:
            debug_log(f"vault relation snapshot failed: {exc}", "vault")
            return
        relations: dict[str, set[str]] = {node.id: set() for node in nodes}
        for node in nodes:
            if node.parent_id and node.parent_id != "root":
                relations.setdefault(node.id, set()).add(node.parent_id)
                relations.setdefault(node.parent_id, set()).add(node.id)
        with self._lock:
            self._relation_cache = relations

    def _run_plan(self, *, node_ids=None, full_sweep=True) -> SyncPlan:
        plan = plan_sync(self.store, self.cfg, node_ids=node_ids, full_sweep=full_sweep)
        if getattr(self.cfg, "obsidian_write_mode", "dry_run") == "dry_run":
            print_sync_plan(plan)
        elif getattr(self.cfg, "obsidian_write_mode", "dry_run") == "on":
            apply_sync(plan, self.cfg)
        self._refresh_relation_cache()
        return plan

    def start(self) -> None:
        if self._thread is not None or getattr(self.cfg, "obsidian_write_mode", "dry_run") == "off":
            return
        self._run_plan(full_sweep=True)
        self._thread = threading.Thread(target=self._loop, name="vault-mirror", daemon=True)
        self._thread.start()
        debug_log("vault mirror worker started", "vault")

    def notify_mutation(self, *, action, node_id, branch) -> None:
        del branch
        try:
            with self._lock:
                self._pending.add(str(node_id))
                # Include the prior graph neighbourhood so renames, moves, and
                # deletions repair links on both sides of the mutation.
                self._pending.update(self._relation_cache.get(str(node_id), set()))
            self._wake.set()
            debug_log(f"vault mutation queued: {action} {node_id}", "vault")
        except Exception as exc:
            debug_log(f"vault mutation queue failed: {exc}", "vault")

    def request_full_sweep(self) -> SyncPlan:
        return self._run_plan(full_sweep=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            if self._stop.is_set():
                break
            if not self._wake.is_set():
                continue
            self._wake.clear()
            if self._stop.wait(self.debounce_seconds):
                break
            with self._lock:
                pending = set(self._pending)
                self._pending.clear()
            if pending:
                self._run_plan(node_ids=pending, full_sweep=False)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.debounce_seconds + 0.5))
            self._thread = None
        debug_log("vault mirror worker stopped", "vault")
