"""Bounded reads of the School branch for school-specific consumers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..debug import debug_log
from ..utils.location import get_location_context_with_timezone
from .graph import BRANCH_SCHOOL, MAX_TRAVERSAL_DEPTH, GraphMemoryStore

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


SCHOOL_CONTEXT_MAX_CHARS = 20_000


def school_local_now(cfg) -> datetime:
    """Resolve the user's local time through the shared location settings."""
    now_utc = datetime.now(timezone.utc)
    tz_name = None
    if bool(getattr(cfg, "location_enabled", True)):
        try:
            _, tz_name = get_location_context_with_timezone(
                config_ip=getattr(cfg, "location_ip_address", None),
                auto_detect=getattr(cfg, "location_auto_detect", True),
                resolve_cgnat_public_ip=getattr(
                    cfg, "location_cgnat_resolve_public_ip", True
                ),
                location_cache_minutes=getattr(cfg, "location_cache_minutes", 60),
                manual_city=getattr(cfg, "location_manual_city", None),
                manual_region=getattr(cfg, "location_manual_region", None),
                manual_country=getattr(cfg, "location_manual_country", None),
                manual_timezone=getattr(cfg, "location_manual_timezone", None),
            )
        except Exception as exc:
            debug_log(
                f"school timezone lookup failed: {type(exc).__name__}",
                "school",
            )
    if tz_name and ZoneInfo is not None:
        try:
            return now_utc.astimezone(ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            pass
    return now_utc.astimezone()


def read_school_branch(
    db_path: str,
    *,
    max_chars: int = SCHOOL_CONTEXT_MAX_CHARS,
) -> dict[str, Any]:
    """Return a bounded, JSON-ready snapshot of populated School nodes."""
    store = GraphMemoryStore(str(db_path))
    try:
        tree = store.get_subtree(BRANCH_SCHOOL, max_depth=MAX_TRAVERSAL_DEPTH)
    finally:
        store.close()

    nodes: list[dict[str, str]] = []
    remaining = max(0, int(max_chars))

    def _walk(branch: dict[str, Any], *, root: bool = False) -> None:
        nonlocal remaining
        if not branch or remaining <= 0:
            return
        raw_node = branch.get("node") or {}
        name = str(raw_node.get("name") or "").strip()
        description = str(raw_node.get("description") or "").strip()
        data = str(raw_node.get("data") or "").strip()

        # The seeded School root's stock description is taxonomy metadata,
        # not school content. Descendant labels and descriptions can carry
        # useful context even before a node has facts of its own.
        if data or (not root and (name or description)):
            record: dict[str, str] = {}
            for key, value in (
                ("name", name),
                ("description", description),
                ("data", data),
            ):
                record[key] = value[:remaining]
                remaining -= len(record[key])
            if any(record.values()):
                nodes.append(record)

        for child in branch.get("children") or []:
            _walk(child)

    _walk(tree, root=True)
    return {"branch": BRANCH_SCHOOL, "nodes": nodes}
