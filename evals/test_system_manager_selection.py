"""Routing eval for the opt-in system-management tool description."""

from __future__ import annotations

import pytest

from conftest import requires_judge_llm
from helpers import JUDGE_MODEL


@pytest.mark.eval
@requires_judge_llm
def test_package_install_request_selects_system_manager(mock_config) -> None:
    from jarvis.llm import get_llm_backend
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_system_management_tool
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    original = BUILTIN_TOOLS.get("systemManager")
    try:
        configure_system_management_tool(
            type("Cfg", (), {"system_management_enabled": True})()
        )
        selected = select_tools(
            query="Install Microsoft PowerToys on this computer",
            builtin_tools=BUILTIN_TOOLS,
            mcp_tools={},
            strategy=ToolSelectionStrategy.LLM,
            llm_backend=get_llm_backend(mock_config),
            llm_model=JUDGE_MODEL,
            llm_timeout_sec=15.0,
        )
    finally:
        if original is None:
            BUILTIN_TOOLS.pop("systemManager", None)
        else:
            BUILTIN_TOOLS["systemManager"] = original

    assert "systemManager" in selected
