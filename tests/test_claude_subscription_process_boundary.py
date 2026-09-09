"""The Claude Agent SDK stays inside the subscription sidecar process."""

import ast
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src" / "jarvis"
REPO = SRC.parent.parent
ALLOWED_IMPORTER = SRC / "llm" / "claude_subscription_sidecar.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _imports_sdk(path: Path) -> bool:
    return any(
        name == "claude_agent_sdk" or name.startswith("claude_agent_sdk.")
        for name in _imports(path)
    )


def test_only_the_sidecar_entry_point_imports_claude_agent_sdk():
    offenders = [
        str(path.relative_to(SRC.parent.parent))
        for path in SRC.rglob("*.py")
        if path != ALLOWED_IMPORTER and _imports_sdk(path)
    ]
    assert not offenders, (
        "these files import claude_agent_sdk outside the sidecar process "
        f"boundary: {offenders}"
    )


def test_the_sidecar_entry_point_imports_claude_agent_sdk():
    assert ALLOWED_IMPORTER.is_file()
    assert _imports_sdk(ALLOWED_IMPORTER)


def test_main_and_sidecar_dependencies_are_declared_separately():
    main_requirements = (REPO / "requirements.txt").read_text(encoding="utf-8")
    sidecar_requirements = (
        REPO / "requirements-claude-sidecar.txt"
    ).read_text(encoding="utf-8")

    assert "mcp==1.13.1" in main_requirements
    assert "claude-agent-sdk" not in main_requirements
    assert "claude-agent-sdk==" in sidecar_requirements
