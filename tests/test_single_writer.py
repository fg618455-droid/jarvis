"""Architecture guard: one owner for the graph memory store.

`jarvis.db` carries the diary and the node graph, and today several components
open it for writing: five `GraphMemoryStore` constructions plus the memory
viewer, which runs as its own Flask process in development. Concurrent cron
runs would turn that into a corruption risk, so the core has to become the
single writer and everything else has to read through the API.

The clean state is asserted as a strict xfail: it fails today, and the day it
passes the marker must come off (T-016).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

STORE_CLASS = "GraphMemoryStore"

# Every component that opens the graph store for itself today. T-016 and T-019
# collapse these onto a single owner in the core; until then this list may only
# ever shrink.
KNOWN_CONSTRUCTION_SITES = frozenset({
    "desktop_app/memory_viewer.py",
    "jarvis/daemon.py",
    "jarvis/memory/conversation.py",
    "jarvis/reply/engine.py",
})


def _construction_sites() -> dict[str, list[int]]:
    """Map each module to the lines where it constructs the store."""
    sites: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == STORE_CLASS:
                lines.append(node.lineno)
        if lines:
            sites[path.relative_to(SRC).as_posix()] = sorted(lines)
    return sites


def test_no_new_graph_store_owners():
    """A module that does not open the graph store today must not start."""
    sites = _construction_sites()
    new = set(sites) - KNOWN_CONSTRUCTION_SITES
    assert not new, (
        "New GraphMemoryStore construction in: "
        + ", ".join(f"{mod}:{sites[mod]}" for mod in sorted(new))
        + ". Read through the owning service instead of opening the database again."
    )


def test_known_construction_sites_are_still_there():
    """Keep the allowlist honest: a converted module must leave it."""
    sites = _construction_sites()
    stale = KNOWN_CONSTRUCTION_SITES - set(sites)
    assert not stale, (
        "These modules no longer construct GraphMemoryStore: "
        + ", ".join(sorted(stale))
        + ". Remove them from KNOWN_CONSTRUCTION_SITES so the guard keeps its teeth."
    )


@pytest.mark.xfail(
    strict=True,
    reason="T-016 makes the core the only writer; the memory viewer becomes an API client",
)
def test_exactly_one_component_opens_the_graph_store():
    sites = _construction_sites()
    assert len(sites) == 1, (
        "GraphMemoryStore is constructed in "
        + ", ".join(f"{mod}:{lines}" for mod, lines in sorted(sites.items()))
    )
