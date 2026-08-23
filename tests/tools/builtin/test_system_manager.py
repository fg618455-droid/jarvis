"""Behaviour tests for the opt-in structured systemManager builtin."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from jarvis.config import load_settings
from jarvis.security.gate import SecurityGate
from jarvis.tools.base import ToolContext
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries


def _ctx(cfg) -> ToolContext:
    return ToolContext(None, cfg, "", "", "", 0, lambda _message: None)


def _completed(stdout: str = "ok") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


@pytest.fixture
def tool():
    from jarvis.tools.builtin.system_manager import SystemManagerTool

    return SystemManagerTool()


@pytest.fixture(autouse=True)
def _reset_security_gate():
    SecurityGate.reset_instance()
    yield
    SecurityGate.reset_instance()


class TestPackages:
    @pytest.mark.parametrize(
        ("operation", "args", "expected"),
        [
            (
                "listInstalledPackages",
                {},
                ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
            ),
            (
                "installPackage",
                {"packageId": "Microsoft.PowerToys"},
                [
                    "winget", "install", "--id", "Microsoft.PowerToys", "--exact",
                    "--accept-package-agreements", "--accept-source-agreements",
                    "--disable-interactivity",
                ],
            ),
            (
                "uninstallPackage",
                {"packageId": "Microsoft.PowerToys"},
                [
                    "winget", "uninstall", "--id", "Microsoft.PowerToys", "--exact",
                    "--disable-interactivity",
                ],
            ),
        ],
    )
    def test_exact_winget_argument_vector_reaches_the_os(
        self, tool, mock_config, operation, args, expected
    ) -> None:
        with patch("jarvis.tools.builtin.system_manager.subprocess.run", return_value=_completed()) as run:
            result = tool.run({"operation": operation, **args}, _ctx(mock_config))

        assert result.success is True
        run.assert_called_once_with(
            expected,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            shell=False,
        )

    @pytest.mark.parametrize("package_id", ["", "Power Toys", "x && calc", "--source=evil"])
    def test_package_id_is_a_winget_id_not_free_text(self, tool, mock_config, package_id) -> None:
        with patch("jarvis.tools.builtin.system_manager.subprocess.run") as run:
            result = tool.run(
                {"operation": "installPackage", "packageId": package_id}, _ctx(mock_config)
            )

        assert result.success is False
        run.assert_not_called()


class TestFiles:
    def test_list_files_uses_the_exact_resolved_directory(self, tool, mock_config, tmp_path) -> None:
        (tmp_path / "b.txt").write_text("b", encoding="utf-8")
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")

        with patch.object(
            Path,
            "iterdir",
            autospec=True,
            return_value=[tmp_path / "b.txt", tmp_path / "a.txt"],
        ) as iterdir:
            result = tool.run(
                {"operation": "listFiles", "path": str(tmp_path)}, _ctx(mock_config)
            )

        assert result.success is True
        iterdir.assert_called_once_with(tmp_path.resolve())
        assert "a.txt" in result.reply_text
        assert "b.txt" in result.reply_text

    def test_read_file_calls_read_text_on_the_exact_resolved_path(
        self, tool, mock_config, tmp_path
    ) -> None:
        target = tmp_path / "note.txt"
        target.write_text("hello", encoding="utf-8")

        with patch.object(Path, "read_text", autospec=True, return_value="hello") as read:
            result = tool.run({"operation": "readFile", "path": str(target)}, _ctx(mock_config))

        assert result.success is True
        read.assert_called_once_with(target.resolve(), encoding="utf-8", errors="replace")

    def test_write_file_calls_write_text_on_the_exact_resolved_path(
        self, tool, mock_config, tmp_path
    ) -> None:
        target = tmp_path / "outside-home" / "note.txt"

        with patch.object(Path, "mkdir", autospec=True) as mkdir, patch.object(
            Path, "write_text", autospec=True, return_value=5
        ) as write:
            result = tool.run(
                {"operation": "writeFile", "path": str(target), "content": "hello"},
                _ctx(mock_config),
            )

        assert result.success is True
        mkdir.assert_called_once_with(target.parent.resolve(), parents=True, exist_ok=True)
        write.assert_called_once_with(target.resolve(), "hello", encoding="utf-8")

    def test_append_file_opens_the_exact_resolved_path(self, tool, mock_config, tmp_path) -> None:
        target = tmp_path / "note.txt"
        handle = MagicMock()
        handle.__enter__.return_value = handle

        with patch.object(Path, "mkdir", autospec=True), patch.object(
            Path, "open", autospec=True, return_value=handle
        ) as opened:
            result = tool.run(
                {"operation": "appendFile", "path": str(target), "content": "more"},
                _ctx(mock_config),
            )

        assert result.success is True
        opened.assert_called_once_with(target.resolve(), "a", encoding="utf-8", errors="replace")
        handle.write.assert_called_once_with("more")

    def test_delete_file_calls_unlink_on_the_exact_resolved_path(
        self, tool, mock_config, tmp_path
    ) -> None:
        target = tmp_path / "note.txt"
        target.write_text("hello", encoding="utf-8")

        with patch.object(Path, "unlink", autospec=True) as unlink:
            result = tool.run({"operation": "deleteFile", "path": str(target)}, _ctx(mock_config))

        assert result.success is True
        unlink.assert_called_once_with(target.resolve())

    @pytest.mark.parametrize(
        "target",
        [
            r"C:\Windows\Temp\jarvis.txt",
            r"C:\Program Files\Jarvis\jarvis.txt",
            r"C:\Program Files (x86)\Jarvis\jarvis.txt",
            r"C:\ProgramData\Jarvis\jarvis.txt",
            r"C:\Boot\BCD",
            r"C:\Recovery\state.txt",
            r"C:\System Volume Information\state.txt",
            r"C:\$Recycle.Bin\state.txt",
            r"C:\bootmgr",
            r"C:\pagefile.sys",
            r"C:\hiberfil.sys",
            r"C:\swapfile.sys",
            r"\\?\C:\Windows\Temp\jarvis.txt",
            r"\\.\C:\ProgramData\Jarvis\jarvis.txt",
            r"\\localhost\C$\Windows\Temp\jarvis.txt",
        ],
    )
    def test_hard_denied_windows_path_is_refused_before_any_io(
        self, tool, mock_config, target
    ) -> None:
        with patch.object(Path, "resolve", autospec=True) as resolve, patch.object(
            Path, "write_text", autospec=True
        ) as write, patch(
            "jarvis.tools.builtin.system_manager.subprocess.run"
        ) as run:
            result = tool.run(
                {"operation": "writeFile", "path": target, "content": "blocked"},
                _ctx(mock_config),
            )

        assert result.success is False
        assert "hard-denied" in (result.reply_text or "").casefold()
        resolve.assert_not_called()
        write.assert_not_called()
        run.assert_not_called()

    def test_resolved_alias_into_a_hard_denied_root_is_refused(
        self, tool, mock_config
    ) -> None:
        alias = Path(r"C:\Users\Public\system-link\note.txt")
        with patch.object(
            Path, "resolve", autospec=True, return_value=Path(r"C:\Windows\note.txt")
        ), patch.object(Path, "write_text", autospec=True) as write:
            result = tool.run(
                {"operation": "writeFile", "path": str(alias), "content": "blocked"},
                _ctx(mock_config),
            )

        assert result.success is False
        assert "hard-denied" in (result.reply_text or "").casefold()
        write.assert_not_called()

    @pytest.mark.parametrize(
        "target",
        [
            r"C:\Users\Public\note.txt:alternate",
            "C:\\Users\\Public\\note.txt ",
            "C:\\Users\\Public\\note.txt.",
        ],
    )
    def test_ambiguous_windows_path_syntax_is_refused(
        self, tool, mock_config, target
    ) -> None:
        with patch.object(Path, "resolve", autospec=True) as resolve, patch.object(
            Path, "write_text", autospec=True
        ) as write:
            result = tool.run(
                {"operation": "writeFile", "path": target, "content": "blocked"},
                _ctx(mock_config),
            )

        assert result.success is False
        resolve.assert_not_called()
        write.assert_not_called()


class FakeWinReg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_DWORD = 4

    def __init__(self, value: int = 1) -> None:
        self.value = value
        self.calls = []
        self.key = MagicMock()
        self.key.__enter__.return_value = self.key

    def OpenKey(self, *args):
        self.calls.append(("OpenKey", args))
        return self.key

    def QueryValueEx(self, *args):
        self.calls.append(("QueryValueEx", args))
        return self.value, self.REG_DWORD

    def SetValueEx(self, *args):
        self.calls.append(("SetValueEx", args))


class TestSettings:
    def test_get_dark_mode_reads_only_the_named_personalisation_value(self, mock_config) -> None:
        from jarvis.tools.builtin.system_manager import SystemManagerTool

        registry = FakeWinReg(value=0)
        result = SystemManagerTool(registry=registry).run(
            {"operation": "getDarkMode"}, _ctx(mock_config)
        )

        assert result.success is True
        assert registry.calls == [
            (
                "OpenKey",
                (
                    registry.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                    0,
                    registry.KEY_READ,
                ),
            ),
            ("QueryValueEx", (registry.key, "AppsUseLightTheme")),
        ]

    def test_set_dark_mode_writes_only_the_two_named_personalisation_values(
        self, mock_config
    ) -> None:
        from jarvis.tools.builtin.system_manager import SystemManagerTool

        registry = FakeWinReg()
        result = SystemManagerTool(registry=registry).run(
            {"operation": "setDarkMode", "enabled": True}, _ctx(mock_config)
        )

        assert result.success is True
        assert registry.calls == [
            (
                "OpenKey",
                (
                    registry.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                    0,
                    registry.KEY_SET_VALUE,
                ),
            ),
            (
                "SetValueEx",
                (registry.key, "AppsUseLightTheme", 0, registry.REG_DWORD, 0),
            ),
            (
                "SetValueEx",
                (registry.key, "SystemUsesLightTheme", 0, registry.REG_DWORD, 0),
            ),
        ]

    def test_get_power_plan_uses_the_exact_powercfg_vector(self, tool, mock_config) -> None:
        with patch("jarvis.tools.builtin.system_manager.subprocess.run", return_value=_completed()) as run:
            result = tool.run({"operation": "getPowerPlan"}, _ctx(mock_config))

        assert result.success is True
        assert run.call_args.args[0] == ["powercfg", "/getactivescheme"]
        assert run.call_args.kwargs["shell"] is False

    @pytest.mark.parametrize(
        ("plan", "guid"),
        [
            ("balanced", "381b4222-f694-41f0-9685-ff5bb260df2e"),
            ("powerSaver", "a1841308-3541-4fab-bc81-f71556f20b4a"),
            ("highPerformance", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"),
        ],
    )
    def test_set_power_plan_maps_enum_to_an_exact_guid(
        self, tool, mock_config, plan, guid
    ) -> None:
        with patch("jarvis.tools.builtin.system_manager.subprocess.run", return_value=_completed()) as run:
            result = tool.run(
                {"operation": "setPowerPlan", "powerPlan": plan}, _ctx(mock_config)
            )

        assert result.success is True
        assert run.call_args.args[0] == ["powercfg", "/setactive", guid]
        assert run.call_args.kwargs["shell"] is False


class DecisionChannel:
    is_available = True

    def __init__(self, decision: bool) -> None:
        self.decision = decision
        self.requests = []

    def ask(self, name, args):
        self.requests.append((name, args))
        return self.decision


class TestConfigurationAndGate:
    def test_system_management_defaults_off_and_loads_from_real_config(
        self, tmp_path, monkeypatch
    ) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
        assert load_settings().system_management_enabled is False

        config_path.write_text(json.dumps({"system_management_enabled": True}), encoding="utf-8")
        assert load_settings().system_management_enabled is True

    def test_registration_follows_the_opt_in_flag(self) -> None:
        from jarvis.tools.registry import configure_system_management_tool

        original = BUILTIN_TOOLS.get("systemManager")
        try:
            configure_system_management_tool(SimpleNamespace(system_management_enabled=False))
            assert "systemManager" not in BUILTIN_TOOLS
            configure_system_management_tool(SimpleNamespace(system_management_enabled=True))
            assert BUILTIN_TOOLS["systemManager"].name == "systemManager"
        finally:
            if original is None:
                BUILTIN_TOOLS.pop("systemManager", None)
            else:
                BUILTIN_TOOLS["systemManager"] = original

    @pytest.mark.parametrize(
        "operation",
        ["listInstalledPackages", "listFiles", "readFile", "getDarkMode", "getPowerPlan"],
    )
    def test_critical_gate_does_not_confirm_inspection_actions(self, operation) -> None:
        channel = DecisionChannel(False)
        gate = SecurityGate(
            level="critical", channels={"desktop": channel}, confirm_channels=["desktop"]
        )

        assert gate.confirm("systemManager", {"operation": operation}) is True
        assert channel.requests == []

    @pytest.mark.parametrize(
        "operation",
        [
            "installPackage", "uninstallPackage", "writeFile", "appendFile",
            "deleteFile", "setDarkMode", "setPowerPlan",
        ],
    )
    def test_critical_gate_confirms_every_mutating_action(self, operation) -> None:
        channel = DecisionChannel(False)
        gate = SecurityGate(
            level="critical", channels={"desktop": channel}, confirm_channels=["desktop"]
        )

        assert gate.confirm("systemManager", {"operation": operation}) is False
        assert channel.requests == [("systemManager", {"operation": operation})]

    @pytest.mark.parametrize(
        ("operation", "args", "os_patch"),
        [
            ("installPackage", {"packageId": "Microsoft.PowerToys"}, "subprocess"),
            ("writeFile", {"path": "placeholder", "content": "hello"}, "filesystem"),
            ("setPowerPlan", {"powerPlan": "balanced"}, "subprocess"),
        ],
    )
    @pytest.mark.parametrize("approved", [False, True])
    def test_full_round_trip_requires_and_honours_confirmation(
        self, mock_config, tmp_path, operation, args, os_patch, approved
    ) -> None:
        from jarvis.tools.registry import configure_system_management_tool

        if operation == "writeFile":
            args = {**args, "path": str(tmp_path / "outside" / "note.txt")}
        channel = DecisionChannel(approved)
        SecurityGate(
            level="critical", channels={"desktop": channel}, confirm_channels=["desktop"]
        )
        cfg = replace(
            mock_config, security_level="critical", system_management_enabled=True
        )
        original = BUILTIN_TOOLS.get("systemManager")
        configure_system_management_tool(cfg)
        try:
            with patch(
                "jarvis.tools.builtin.system_manager.subprocess.run", return_value=_completed()
            ) as run, patch.object(Path, "write_text", autospec=True, return_value=5) as write:
                result = run_tool_with_retries(
                    None, cfg, "systemManager", {"operation": operation, **args}, "", "", ""
                )
        finally:
            if original is None:
                BUILTIN_TOOLS.pop("systemManager", None)
            else:
                BUILTIN_TOOLS["systemManager"] = original

        assert result.success is approved
        assert channel.requests == [("systemManager", {"operation": operation, **args})]
        if approved and os_patch == "subprocess":
            run.assert_called_once()
        elif approved:
            write.assert_called_once()
        elif not approved:
            run.assert_not_called()
            write.assert_not_called()

    def test_hard_deny_cannot_be_overridden_by_confirmation(self, mock_config) -> None:
        from jarvis.tools.registry import configure_system_management_tool

        channel = DecisionChannel(True)
        SecurityGate(
            level="critical", channels={"desktop": channel}, confirm_channels=["desktop"]
        )
        cfg = replace(
            mock_config, security_level="critical", system_management_enabled=True
        )
        original = BUILTIN_TOOLS.get("systemManager")
        configure_system_management_tool(cfg)
        try:
            with patch.object(Path, "write_text", autospec=True) as write:
                result = run_tool_with_retries(
                    None,
                    cfg,
                    "systemManager",
                    {"operation": "writeFile", "path": r"C:\Windows\x", "content": "x"},
                    "",
                    "",
                    "",
                )
        finally:
            if original is None:
                BUILTIN_TOOLS.pop("systemManager", None)
            else:
                BUILTIN_TOOLS["systemManager"] = original

        assert channel.requests
        assert result.success is False
        write.assert_not_called()

    def test_system_management_flag_is_exposed_in_metadata(self) -> None:
        from jarvis.config_metadata import FIELD_METADATA

        field = next(
            meta for meta in FIELD_METADATA if meta.key == "system_management_enabled"
        )
        assert field.field_type == "bool"
        assert field.category == "features"


def test_tool_source_contains_no_shell_true_or_free_form_command_field() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "src/jarvis/tools/builtin/system_manager.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert '"command"' not in source
    assert "'command'" not in source
