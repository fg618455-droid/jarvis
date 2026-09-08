"""Architecture guard: no local LLM runtime in the production source.

Once the reply path runs on subscription providers, a leftover reference to a
local runtime is either dead weight or a hidden fallback, and a hidden fallback
makes cost, data flow and answer quality unpredictable.

The clean state is asserted as a strict xfail: it fails today, and the day it
passes the marker must come off (T-045). Whisper and Piper are out of scope:
they are speech models behind the voice isolation boundary, not chat runtimes.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

LOCAL_RUNTIME_PATTERN = re.compile(r"ollama|lm ?studio|llama\.cpp|localai|vllm", re.IGNORECASE)

# Every production module that still names a local chat runtime. The list may
# only ever shrink: phases 5 and 6 empty it, and nothing may be added.
KNOWN_REFERENCES = frozenset({
    "desktop_app/app.py",
    "desktop_app/settings_window.py",
    "desktop_app/setup_wizard.py",
    "jarvis/config.py",
    "jarvis/listening/intent_judge.py",
    "jarvis/listening/listener.py",
    "jarvis/llm/__init__.py",
    "jarvis/llm/backend.py",
    "jarvis/llm/factory.py",
    "jarvis/llm/ollama.py",
    "jarvis/llm/openai_compatible.py",
    "jarvis/memory/graph_ops.py",
    "jarvis/reply/engine.py",
    "jarvis/reply/enrichment.py",
    "jarvis/tools/registry.py",
})


def _modules_naming_a_local_runtime() -> set[str]:
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        if LOCAL_RUNTIME_PATTERN.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(SRC).as_posix())
    return found


def test_no_new_local_runtime_references():
    """A module that does not name a local runtime today must not start."""
    new = _modules_naming_a_local_runtime() - KNOWN_REFERENCES
    assert not new, (
        "New local LLM runtime reference in: " + ", ".join(sorted(new))
        + ". Reasoning belongs to the subscription providers; there is no local fallback."
    )


def test_cleared_modules_leave_the_allowlist():
    """Keep the allowlist honest: a cleaned module must leave it."""
    stale = KNOWN_REFERENCES - _modules_naming_a_local_runtime()
    assert not stale, (
        "These modules no longer name a local runtime: " + ", ".join(sorted(stale))
        + ". Remove them from KNOWN_REFERENCES so the guard keeps its teeth."
    )


@pytest.mark.xfail(
    strict=True,
    reason="T-041 and T-045 remove the local model paths and their configuration",
)
def test_production_source_names_no_local_runtime():
    found = _modules_naming_a_local_runtime()
    assert not found, "Local LLM runtime named in: " + ", ".join(sorted(found))
