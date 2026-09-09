"""Deterministic graph-node rendering for the Obsidian mirror."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterable

import yaml

from ..graph import FIXED_BRANCH_IDS, MemoryNode


END_MARKER = "<!-- jarvis:end -->"
_FORBIDDEN = frozenset('#^[]|<>:"\\/?*')
_WHITESPACE = re.compile(r"\s+")
_DOT_RUN = re.compile(r"\.{2,}")


def _slug(value: str, *, limit: int = 60) -> str:
    cleaned = "".join(
        " " if char in "\r\n\t" else char
        for char in unicodedata.normalize("NFKC", str(value))
        if char not in _FORBIDDEN and unicodedata.category(char) != "Cc"
    )
    cleaned = _DOT_RUN.sub(".", _WHITESPACE.sub(" ", cleaned).strip())
    cleaned = cleaned.rstrip(". ")
    if len(cleaned) > limit:
        candidate = cleaned[:limit].rstrip()
        if " " in candidate and len(cleaned) > limit and cleaned[limit:limit + 1] not in ("", " "):
            candidate = candidate.rsplit(" ", 1)[0]
        cleaned = candidate.rstrip(". ")
    return cleaned or "Untitled"


def filename_for_node(node: MemoryNode, branch_label: str) -> str:
    """Return the flat, collision-resistant filename for a graph node."""
    short_id = node.id if node.id in FIXED_BRANCH_IDS else node.id[:8]
    name = _slug(node.name)
    if node.id in FIXED_BRANCH_IDS:
        base = f"{name} ({short_id})"
    else:
        branch = _slug(branch_label)
        base = f"{branch} — {name} ({short_id})"

    max_base = 116  # under 120 including the .md suffix
    if len(base) > max_base:
        suffix = f" ({short_id})"
        prefix = "" if node.id in FIXED_BRANCH_IDS else f"{_slug(branch_label)} — "
        room = max(1, max_base - len(prefix) - len(suffix))
        name = _slug(node.name, limit=room)
        base = f"{prefix}{name}{suffix}"
        if len(base) > max_base:
            base = base[:max_base - len(suffix)].rstrip(". ") + suffix
    return base + ".md"


def split_protected_region(content: str) -> tuple[str, str]:
    """Split machine content from the byte-for-byte user-owned tail."""
    if END_MARKER not in content:
        return content, ""
    machine, protected = content.split(END_MARKER, 1)
    return machine, protected


def _frontmatter(node: MemoryNode, branch_id: str) -> str:
    created = str(node.created_at or "")[:10]
    values = {
        "jarvis_managed": True,
        "jarvis_node_id": str(node.id),
        "jarvis_branch": str(branch_id),
        "created": created,
        "updated": str(node.updated_at or ""),
        "access_count": int(node.access_count),
        "tokens": int(node.data_token_count),
        "tags": ["jarvis/memory", f"jarvis/{branch_id}"],
    }
    dumped = yaml.safe_dump(
        values,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).rstrip()
    return f"---\n{dumped}\n---\n"


def _escape_fact(line: str) -> str:
    return line.replace("[[", r"\[\[")


def render_node(
    node: MemoryNode,
    branch_id: str,
    branch_label: str,
    *,
    parent: MemoryNode | None = None,
    children: Iterable[MemoryNode] = (),
    protected_tail: str | None = None,
) -> str:
    """Render the complete managed note while retaining a supplied user tail."""
    parts = [_frontmatter(node, branch_id), f"\n# {node.name}\n"]
    if node.description:
        parts.append(f"\n{node.description}\n")
    facts = str(node.data or "").splitlines()
    if facts:
        parts.append("\n")
        parts.extend(f"- {_escape_fact(line)}\n" for line in facts)

    related: list[str] = []
    if parent is not None and parent.id != "root":
        stem = Path(filename_for_node(parent, branch_label)).stem
        related.append(f"- Parent: [[{stem}|{parent.name}]]")
    for child in children:
        stem = Path(filename_for_node(child, branch_label)).stem
        related.append(f"- Children: [[{stem}|{child.name}]]")
    if related:
        parts.append("\n## Related\n\n")
        parts.append("\n".join(related) + "\n")

    tail = "\n" if protected_tail is None else protected_tail
    parts.append(f"\n{END_MARKER}{tail}")
    return "".join(parts)


def parse_frontmatter(content: str) -> dict:
    """Parse a leading YAML frontmatter mapping, returning empty on failure."""
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---\n", 4)
    if end < 0:
        return {}
    try:
        parsed = yaml.safe_load(content[4:end])
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_managed_markdown(content: str) -> bool:
    return parse_frontmatter(content).get("jarvis_managed") is True


def managed_node_id(content: str) -> str | None:
    metadata = parse_frontmatter(content)
    if metadata.get("jarvis_managed") is not True:
        return None
    value = metadata.get("jarvis_node_id")
    return str(value) if value is not None else None
