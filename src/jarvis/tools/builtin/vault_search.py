"""Read-only search tool for the configured local Obsidian vault."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...memory.vault.index import VaultIndex, format_hits_for_prompt
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


class VaultSearchTool(Tool):
    @property
    def name(self) -> str:
        return "vaultSearch"

    @property
    def description(self) -> str:
        return "Search the user's configured local Obsidian notes by keyword. Read-only."

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to find in local vault note titles, tags, and text",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results, from 1 to 20",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        query = str((args or {}).get("query", "")).strip()
        if not query:
            return ToolExecutionResult(False, "🔎 Vault search requires a query.")
        vault_path = getattr(context.cfg, "obsidian_vault_path", None)
        if not vault_path:
            return ToolExecutionResult(False, "🔒 Vault search is not configured.")
        if not getattr(context.cfg, "obsidian_read_enabled", True):
            return ToolExecutionResult(False, "🔒 Vault reading is disabled.")
        try:
            limit = max(1, min(int((args or {}).get("limit", 5)), 20))
        except (TypeError, ValueError):
            limit = 5
        context.user_print(f"  📚 Vault search: {query}")
        index = VaultIndex(
            vault_path,
            getattr(context.cfg, "obsidian_memory_folder", "Jarvis") or "Jarvis",
            getattr(context.cfg, "obsidian_index_max_file_kb", 512),
        )
        hits = index.search(query, limit=limit)
        if not hits:
            return ToolExecutionResult(True, "🔎 No matching vault notes found.")
        return ToolExecutionResult(True, format_hits_for_prompt(hits))
