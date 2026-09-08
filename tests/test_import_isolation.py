"""Architecture guard: `jarvis` must not import `desktop_app`.

`desktop_app.spec.md` states that the desktop app is a separate layer and that
`jarvis` has no knowledge of it. The core is heading for a headless service that
several clients talk to, so every one of these imports has to become a port the
client plugs into.

The clean state is asserted as a strict xfail: it fails today, and the day it
passes the marker must come off (T-017).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
JARVIS_SRC = REPO_ROOT / "src" / "jarvis"

FORBIDDEN_PACKAGE = "desktop_app"

# Every import of `desktop_app` from inside `jarvis` today. All of them pull in
# the face widget to drive the avatar's state. T-017 replaces them with a
# notifications port; until then this list may only ever shrink.
KNOWN_VIOLATIONS = frozenset({
    "daemon.py",
    "listening/listener.py",
    "listening/state_manager.py",
    "output/tts.py",
    "reply/engine.py",
})


def _forbidden_imports() -> dict[str, list[int]]:
    """Map each offending module to the lines that import `desktop_app`."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(JARVIS_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == FORBIDDEN_PACKAGE for a in node.names):
                    lines.append(node.lineno)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root == FORBIDDEN_PACKAGE:
                    lines.append(node.lineno)
        if lines:
            offenders[path.relative_to(JARVIS_SRC).as_posix()] = sorted(lines)
    return offenders


def test_no_new_desktop_app_imports():
    """A module that does not import `desktop_app` today must not start."""
    offenders = _forbidden_imports()
    new = set(offenders) - KNOWN_VIOLATIONS
    assert not new, (
        "New import of desktop_app from jarvis: "
        + ", ".join(f"{mod}:{offenders[mod]}" for mod in sorted(new))
        + ". The core must stay independent of the desktop layer; route the "
        "call through a port instead."
    )


def test_known_violations_are_still_there():
    """Keep the allowlist honest: a resolved module must leave it."""
    offenders = _forbidden_imports()
    stale = KNOWN_VIOLATIONS - set(offenders)
    assert not stale, (
        "These modules no longer import desktop_app: "
        + ", ".join(sorted(stale))
        + ". Remove them from KNOWN_VIOLATIONS so the guard keeps its teeth."
    )


@pytest.mark.xfail(
    strict=True,
    reason="T-017 replaces the face-widget imports with a notifications port",
)
def test_jarvis_does_not_import_desktop_app():
    offenders = _forbidden_imports()
    assert not offenders, (
        "jarvis imports desktop_app in: "
        + ", ".join(f"{mod}:{lines}" for mod, lines in sorted(offenders.items()))
    )
