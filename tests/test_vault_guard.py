from __future__ import annotations

import os

import pytest

from jarvis.memory.vault.guard import VaultWriteError, resolve_managed_path


pytestmark = pytest.mark.unit


def test_direct_child_is_resolved_inside_memory_folder(tmp_path):
    vault = tmp_path / "vault"
    memory = vault / "Jarvis"
    memory.mkdir(parents=True)

    resolved = resolve_managed_path(vault, "Jarvis", "User (user).md")

    assert resolved == memory / "User (user).md"


def test_traversal_cannot_escape_memory_folder(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Jarvis").mkdir(parents=True)

    with pytest.raises(VaultWriteError):
        resolve_managed_path(vault, "Jarvis", "../../../etc/passwd.md")


def test_non_markdown_and_nested_targets_are_refused(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Jarvis").mkdir(parents=True)

    with pytest.raises(VaultWriteError):
        resolve_managed_path(vault, "Jarvis", "note.txt")
    with pytest.raises(VaultWriteError):
        resolve_managed_path(vault, "Jarvis", "nested/note.md")


def test_path_resolving_outside_folder_is_refused(tmp_path):
    vault = tmp_path / "vault"
    memory = vault / "Jarvis"
    memory.mkdir(parents=True)
    outside = vault / "outside.md"
    outside.write_text("private", encoding="utf-8")
    link = memory / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this system")

    with pytest.raises(VaultWriteError):
        resolve_managed_path(vault, "Jarvis", "linked.md")


def test_symlinked_memory_folder_outside_vault_is_refused(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (vault / "Jarvis").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this system")

    with pytest.raises(VaultWriteError):
        resolve_managed_path(vault, "Jarvis", "note.md")


def test_missing_vault_is_refused(tmp_path):
    with pytest.raises(VaultWriteError):
        resolve_managed_path(tmp_path / "missing", "Jarvis", "note.md")
