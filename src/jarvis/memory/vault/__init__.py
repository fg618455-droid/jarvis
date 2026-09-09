"""Local Obsidian vault reader and graph mirror."""

from .guard import VaultWriteError, resolve_managed_path
from .index import VaultHit, VaultIndex, VaultNote, get_vault_index
from .mirror import PlannedChange, SyncPlan, apply_sync, plan_sync

__all__ = [
    "PlannedChange",
    "SyncPlan",
    "VaultHit",
    "VaultIndex",
    "VaultNote",
    "VaultWriteError",
    "apply_sync",
    "get_vault_index",
    "plan_sync",
    "resolve_managed_path",
]
