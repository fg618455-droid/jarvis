"""Read-only command-line preview of a full vault mirror sweep."""

from __future__ import annotations

import sys

from ...config import load_settings
from ..graph import GraphMemoryStore
from .mirror import plan_sync, print_sync_plan


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    cfg = load_settings()
    if not cfg.obsidian_vault_path or not cfg.obsidian_memory_folder:
        print("  ℹ️ Vault mirror: disabled", flush=True)
        print("     · Set 'obsidian_vault_path' in config.json to your vault root", flush=True)
        print("     · Writes stay off until 'obsidian_write_mode' is set to 'on'", flush=True)
        return 0
    store = GraphMemoryStore(cfg.db_path)
    try:
        print_sync_plan(plan_sync(store, cfg, full_sweep=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
