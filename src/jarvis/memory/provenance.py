"""Typed snippets whose retrieval origin remains attached to their text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Iterable, Optional


_KINDS = frozenset({"diary", "graph", "vault", "remio"})


@dataclass(frozen=True)
class MemoryProvenance:
    """The source kind and stable identifier known at retrieval time."""

    kind: str
    identifier: str
    branch: str = ""

    @classmethod
    def diary(cls, entry_date: str) -> "MemoryProvenance":
        return cls("diary", str(entry_date or ""))

    @classmethod
    def graph(cls, node_id: str, branch: str) -> "MemoryProvenance":
        return cls("graph", str(node_id or ""), str(branch or ""))

    @classmethod
    def vault(cls, relative_path: str) -> "MemoryProvenance":
        return cls("vault", str(relative_path or ""))

    @classmethod
    def remio(cls, note_title: str) -> "MemoryProvenance":
        return cls("remio", str(note_title or ""))

    def public_dict(self) -> dict[str, str]:
        """Return the bounded identifier shape safe to place in a tool result."""
        if self.kind not in _KINDS:
            return {"kind": "unknown", "identifier_status": "invalid"}
        if self.kind == "diary":
            try:
                parsed = date.fromisoformat(self.identifier)
            except (TypeError, ValueError):
                return {"kind": "diary", "date_status": "invalid"}
            return {"kind": "diary", "date": parsed.isoformat()}
        if self.kind == "graph":
            node_id = _bounded_identifier(self.identifier)
            branch = _bounded_identifier(self.branch, max_chars=64)
            if not node_id or not branch:
                return {"kind": "graph", "identifier_status": "invalid"}
            return {"kind": "graph", "node_id": node_id, "branch": branch}
        if self.kind == "vault":
            path = _safe_vault_relative_path(self.identifier)
            if path is None:
                return {"kind": "vault", "path_status": "invalid"}
            return {"kind": "vault", "path": path}
        title = _bounded_identifier(self.identifier)
        if not title:
            return {"kind": "remio", "title_status": "invalid"}
        return {"kind": "remio", "title": title}


class RetrievedSnippet(str):
    """String-compatible retrieved text with optional carried provenance."""

    __slots__ = ("provenance",)

    def __new__(
        cls,
        text: str,
        provenance: Optional[MemoryProvenance] = None,
    ) -> "RetrievedSnippet":
        value = super().__new__(cls, str(text or ""))
        value.provenance = provenance
        return value

    @property
    def text(self) -> str:
        return str(self)


def graph_snippet(
    text: str,
    *,
    node_id: str,
    branch: str,
) -> RetrievedSnippet:
    """Build a graph retrieval result at the point its node is known."""
    return RetrievedSnippet(text, MemoryProvenance.graph(node_id, branch))


def provenance_payload(snippets: Iterable[RetrievedSnippet]) -> dict:
    """Serialise carried records without composing a user-facing answer."""
    records: list[dict] = []
    unrecorded = 0
    for snippet in snippets:
        provenance = getattr(snippet, "provenance", None)
        if not isinstance(provenance, MemoryProvenance):
            unrecorded += 1
            continue
        records.append({
            "snippet": str(snippet),
            "provenance": provenance.public_dict(),
        })
    status = "recorded" if records else "not_recorded"
    if records and unrecorded:
        status = "partial"
    return {
        "status": status,
        "records": records,
        "unrecorded_snippet_count": unrecorded,
        "source_claim_policy": "cite_matching_records_only",
        "missing_origin_policy": "report_not_recorded_without_inference",
    }


def redact_vault_paths(
    text: str,
    snippets: Iterable[RetrievedSnippet],
) -> str:
    """Remove disclosed vault paths before a reply enters the hot window."""
    scrubbed = str(text or "")
    for snippet in snippets:
        provenance = getattr(snippet, "provenance", None)
        if not isinstance(provenance, MemoryProvenance) or provenance.kind != "vault":
            continue
        public = provenance.public_dict()
        path = public.get("path")
        if not path:
            continue
        variants = {path, path.replace("/", "\\")}
        for variant in variants:
            scrubbed = re.sub(
                re.escape(variant),
                "[vault path disclosed on request]",
                scrubbed,
                flags=re.IGNORECASE,
            )
    return scrubbed


def _bounded_identifier(value: str, *, max_chars: int = 240) -> str:
    clean = " ".join(str(value or "").split())[:max_chars].strip()
    if not clean or any(ord(char) < 32 for char in clean):
        return ""
    return clean


def _safe_vault_relative_path(value: str) -> Optional[str]:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or PureWindowsPath(raw).drive:
        return None
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    rendered = candidate.as_posix()
    if len(rendered) > 500 or any(ord(char) < 32 for char in rendered):
        return None
    return rendered
