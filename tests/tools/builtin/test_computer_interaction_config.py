from __future__ import annotations

import json
from pathlib import Path

from jarvis.config import load_settings
from jarvis.security.gate import SecurityGate
from jarvis.tools.registry import BUILTIN_TOOLS, configure_computer_interaction_tools


def test_computer_interaction_defaults_off_and_loads_from_a_real_config_file(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    assert load_settings().computer_interaction_enabled is False

    config_path.write_text(json.dumps({"computer_interaction_enabled": True}), encoding="utf-8")
    assert load_settings().computer_interaction_enabled is True


def test_computer_interaction_flag_is_exposed_in_metadata() -> None:
    from jarvis.config_metadata import FIELD_METADATA

    field = next(meta for meta in FIELD_METADATA if meta.key == "computer_interaction_enabled")
    assert field.field_type == "bool"
    assert field.category == "features"


def test_registration_follows_the_opt_in_flag() -> None:
    original = {name: BUILTIN_TOOLS.get(name) for name in ("browserInteract", "desktopInteract")}
    try:
        configure_computer_interaction_tools(type("Cfg", (), {"computer_interaction_enabled": False})())
        assert "browserInteract" not in BUILTIN_TOOLS
        assert "desktopInteract" not in BUILTIN_TOOLS

        configure_computer_interaction_tools(type("Cfg", (), {"computer_interaction_enabled": True})())
        assert BUILTIN_TOOLS["browserInteract"].name == "browserInteract"
        assert BUILTIN_TOOLS["desktopInteract"].name == "desktopInteract"
    finally:
        for name, tool in original.items():
            if tool is None:
                BUILTIN_TOOLS.pop(name, None)
            else:
                BUILTIN_TOOLS[name] = tool


def test_reapplying_enabled_registration_reuses_the_live_controllers() -> None:
    original = {name: BUILTIN_TOOLS.get(name) for name in ("browserInteract", "desktopInteract")}
    enabled = type("Cfg", (), {"computer_interaction_enabled": True})()
    try:
        configure_computer_interaction_tools(enabled)
        first_browser = BUILTIN_TOOLS["browserInteract"]
        first_desktop = BUILTIN_TOOLS["desktopInteract"]

        configure_computer_interaction_tools(enabled)

        assert BUILTIN_TOOLS["browserInteract"] is first_browser
        assert BUILTIN_TOOLS["desktopInteract"] is first_desktop
    finally:
        for name, tool in original.items():
            if tool is None:
                BUILTIN_TOOLS.pop(name, None)
            else:
                BUILTIN_TOOLS[name] = tool


def test_both_public_tools_are_critical_builtins() -> None:
    channel = type("Channel", (), {
        "is_available": True,
        "ask": lambda self, name, args: False,
    })()
    gate = SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])

    assert gate.confirm("browserInteract", {"task": "read"}) is False
    assert gate.confirm("desktopInteract", {"application": "Notepad", "task": "read"}) is False


def test_dependencies_are_pinned_and_no_broad_automation_package_is_added() -> None:
    root = Path(__file__).resolve().parents[3]
    for filename in ("requirements.txt", "requirements-felix.txt"):
        text = (root / filename).read_text(encoding="utf-8")
        assert "playwright==" in text
        assert "pywinauto==" in text
        assert "windows-mcp" not in text.casefold()


def test_launch_scripts_install_the_browser_for_the_pinned_playwright_package() -> None:
    root = Path(__file__).resolve().parents[3]
    for filename in ("scripts/run_windows.ps1", "scripts/run_macos.sh", "scripts/run_linux.sh"):
        text = (root / filename).read_text(encoding="utf-8")
        assert "playwright install chromium" in text


def test_windows_launchers_stop_when_dependency_installation_fails() -> None:
    root = Path(__file__).resolve().parents[3]
    powershell = (root / "scripts/run_windows.ps1").read_text(encoding="utf-8")
    batch = (root / "scripts/run_desktop_app.bat").read_text(encoding="utf-8")

    assert powershell.count("Dependency installation failed.") == 2
    assert "ERROR: Dependency installation failed." in batch
    dependency_error = batch.index("ERROR: Dependency installation failed.")
    assert "exit /b 1" in batch[dependency_error:dependency_error + 200]


def test_new_tool_sources_contain_no_eval_shell_or_coordinate_automation() -> None:
    root = Path(__file__).resolve().parents[3]
    sources = "\n".join(
        (root / filename).read_text(encoding="utf-8")
        for filename in (
            "src/jarvis/tools/builtin/browser_interact.py",
            "src/jarvis/tools/builtin/desktop_interact.py",
        )
    )
    for forbidden in (
        ".evaluate(", "import subprocess", "subprocess.", "os.system(",
        "shell=True", "send_keys(", "click_input(",
    ):
        assert forbidden not in sources
