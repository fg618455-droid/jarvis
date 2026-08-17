"""Bounded retrieval from the local remio knowledge base."""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from typing import Callable, Iterable

from ..debug import debug_log


@dataclass(frozen=True)
class MemoryHit:
    text: str
    source: str
    title: str = ""
    note_id: str = ""


class RemioAdapter:
    """Search remio and read a bounded set of attributable note bodies."""

    def __init__(
        self,
        *,
        executable: str = "remio",
        timeout_sec: float = 2.0,
        max_results: int = 3,
        read_chars: int = 1800,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.timeout_sec = timeout_sec
        self.max_results = max(1, max_results)
        self.read_chars = max(1, read_chars)
        self._run = run

    def _call(self, args: list[str]) -> dict:
        try:
            completed = self._run(
                [self.executable, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
            if completed.returncode != 0:
                return {}
            payload = json.loads(completed.stdout or "{}")
            if isinstance(payload, dict) and payload.get("ok"):
                data = payload.get("data", {})
                return data if isinstance(data, dict) else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            debug_log(f"remio retrieval unavailable ({type(error).__name__})", "memory")
        return {}

    def search(self, query: str) -> list[MemoryHit]:
        query = (query or "").strip()
        if not query:
            return []
        data = self._call([
            "search_notes", "--query", query, "--limit", str(self.max_results),
        ])
        items = data.get("results", []) if isinstance(data, dict) else []
        hits: list[MemoryHit] = []
        for item in items[:self.max_results]:
            if not isinstance(item, dict):
                continue
            note_id = str(item.get("noteId", "") or "").strip()
            if not note_id:
                continue
            note = self._call(["read_note", note_id])
            content = str(note.get("content", "") or "").strip()
            if not content:
                content = str(item.get("preview", "") or "").strip()
            if content:
                hits.append(MemoryHit(
                    content[:self.read_chars],
                    "remio",
                    str(item.get("title", "") or ""),
                    note_id,
                ))
        return hits


def format_hits(hits: Iterable[MemoryHit]) -> str:
    """Format attributable note excerpts for the reply context."""
    parts = []
    for hit in hits:
        label = hit.title or hit.note_id or "note"
        parts.append(f"[Remio: {label}]\n{hit.text}")
    return "\n\n".join(parts)
