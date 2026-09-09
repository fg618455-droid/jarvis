"""Read-only access to provenance carried by retrieved memory snippets."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ...debug import debug_log
from ...memory.provenance import provenance_payload
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


class MemoryProvenanceTool(Tool):
    @property
    def name(self) -> str:
        return "memoryProvenance"

    @property
    def description(self) -> str:
        return (
            "Use only when the user asks where a remembered personal fact came "
            "from, how you know it, or requests the source of recalled memory, "
            "in any language. Do not use merely to recall or list remembered "
            "facts. Returns raw carried source records. Cite only a record whose "
            "snippet supports the questioned fact. If none does, say the origin "
            "is not recorded and never guess one."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(
        self,
        args: Optional[Dict[str, Any]],
        context: ToolContext,
    ) -> ToolExecutionResult:
        del args
        snippets = getattr(context, "memory_snippets", None) or []
        payload = provenance_payload(snippets)
        debug_log(
            "memory provenance returned "
            f"{len(payload['records'])} recorded sources and "
            f"{payload['unrecorded_snippet_count']} unrecorded snippets",
            "memory",
        )
        return ToolExecutionResult(
            success=True,
            reply_text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
