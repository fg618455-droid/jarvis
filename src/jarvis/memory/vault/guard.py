"""Filesystem boundary for the Obsidian vault mirror."""

from __future__ import annotations

from pathlib import Path


class VaultWriteError(RuntimeError):
    """Raised when a requested mirror path is outside the managed folder."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_managed_path(
    vault_root: str | Path,
    memory_folder: str | Path,
    relative_name: str | Path,
) -> Path:
    """Resolve one markdown file that is a direct child of the managed folder.

    Symlinks are followed before containment is checked. The managed folder may
    be absent so a first mirror pass can create it, but the vault root must
    already be an accessible directory.
    """
    try:
        root = Path(vault_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VaultWriteError(f"Vault root cannot be resolved: {exc}") from exc
    if not root.is_dir():
        raise VaultWriteError("Vault root is not a directory")

    folder_arg = Path(str(memory_folder).replace("\\", "/"))
    if folder_arg.is_absolute() or not folder_arg.parts or ".." in folder_arg.parts:
        raise VaultWriteError("Managed folder must be a safe vault-relative path")

    try:
        managed = (root / folder_arg).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise VaultWriteError(f"Managed folder cannot be resolved: {exc}") from exc
    if managed == root or not _is_within(managed, root):
        raise VaultWriteError("Managed folder resolves outside the vault")

    name_arg = Path(relative_name)
    if name_arg.is_absolute() or len(name_arg.parts) != 1:
        raise VaultWriteError("Managed notes must be direct children")
    if name_arg.suffix.casefold() != ".md":
        raise VaultWriteError("Managed notes must be markdown files")

    unresolved = managed / name_arg
    try:
        target = unresolved.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise VaultWriteError(f"Managed note cannot be resolved: {exc}") from exc
    if target.parent != managed or not _is_within(target, managed):
        raise VaultWriteError("Managed note resolves outside the managed folder")
    return target
