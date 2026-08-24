"""Behaviour tests for the openOnComputer builtin.

The tool's job is to put something on the user's screen, so every test here
asserts what reached the operating system: which URL went to the browser,
which executable was started, which path was handed to the platform opener.
"""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.open_on_computer import OpenOnComputerTool


def _ctx(cfg):
    return ToolContext(
        db=None,
        cfg=cfg,
        system_prompt="",
        original_prompt="",
        redacted_text="",
        max_retries=0,
        user_print=lambda _m: None,
        language=None,
    )


@pytest.fixture
def tool():
    return OpenOnComputerTool()


class TestWebsites:
    def test_full_url_reaches_the_browser(self, tool, mock_config):
        with patch("jarvis.tools.builtin.open_on_computer.webbrowser.open",
                   return_value=True) as opened:
            result = tool.run({"target": "https://www.youtube.com/watch?v=abc"}, _ctx(mock_config))

        assert result.success is True
        opened.assert_called_once_with("https://www.youtube.com/watch?v=abc")

    def test_bare_domain_gets_an_https_scheme(self, tool, mock_config):
        with patch("jarvis.tools.builtin.open_on_computer._resolve_application",
                   return_value=None), \
             patch("jarvis.tools.builtin.open_on_computer.webbrowser.open",
                   return_value=True) as opened:
            result = tool.run({"target": "youtube.com"}, _ctx(mock_config))

        assert result.success is True
        opened.assert_called_once_with("https://youtube.com")

    def test_bare_domain_with_a_path_keeps_the_path(self, tool, mock_config):
        with patch("jarvis.tools.builtin.open_on_computer.webbrowser.open",
                   return_value=True) as opened:
            result = tool.run({"target": "www.bbc.co.uk/news"}, _ctx(mock_config))

        assert result.success is True
        opened.assert_called_once_with("https://www.bbc.co.uk/news")

    def test_no_browser_available_is_reported_as_a_failure(self, tool, mock_config):
        with patch("jarvis.tools.builtin.open_on_computer.webbrowser.open",
                   return_value=False):
            result = tool.run({"target": "https://example.com"}, _ctx(mock_config))

        assert result.success is False


class TestApplications:
    def test_an_application_on_path_is_started(self, tool, mock_config, tmp_path):
        exe = tmp_path / "notepad.exe"
        exe.write_text("", encoding="utf-8")

        with patch("jarvis.tools.builtin.open_on_computer.shutil.which",
                   return_value=str(exe)), \
             patch("jarvis.tools.builtin.open_on_computer.subprocess.Popen") as popen:
            result = tool.run({"target": "notepad"}, _ctx(mock_config))

        assert result.success is True
        popen.assert_called_once()
        assert popen.call_args.args[0] == [str(exe)]

    def test_the_operating_system_never_sees_a_shell(self, tool, mock_config, tmp_path):
        exe = tmp_path / "notepad.exe"
        exe.write_text("", encoding="utf-8")

        with patch("jarvis.tools.builtin.open_on_computer.shutil.which",
                   return_value=str(exe)), \
             patch("jarvis.tools.builtin.open_on_computer.subprocess.Popen") as popen:
            tool.run({"target": "notepad"}, _ctx(mock_config))

        assert popen.call_args.kwargs.get("shell", False) is False

    @pytest.mark.parametrize("target", [
        "notepad && del everything",
        "notepad | curl evil.example",
        "notepad; rm -rf ~",
        "$(whoami)",
    ])
    def test_a_target_carrying_shell_syntax_resolves_to_nothing(self, tool, mock_config, target):
        with patch("jarvis.tools.builtin.open_on_computer.subprocess.Popen") as popen, \
             patch("jarvis.tools.builtin.open_on_computer.webbrowser.open") as browser:
            result = tool.run({"target": target}, _ctx(mock_config))

        assert result.success is False
        popen.assert_not_called()
        browser.assert_not_called()

    def test_an_unknown_application_fails_instead_of_opening_a_browser(self, tool, mock_config):
        with patch("jarvis.tools.builtin.open_on_computer._resolve_application",
                   return_value=None), \
             patch("jarvis.tools.builtin.open_on_computer.webbrowser.open") as browser:
            result = tool.run({"target": "definitelynotinstalled"}, _ctx(mock_config))

        assert result.success is False
        browser.assert_not_called()

    def test_a_missing_executable_suffix_target_is_not_read_as_a_domain(self, tool, mock_config):
        """``notepad.exe`` that does not resolve is a missing program, not a host."""
        with patch("jarvis.tools.builtin.open_on_computer._resolve_application",
                   return_value=None), \
             patch("jarvis.tools.builtin.open_on_computer.webbrowser.open") as browser:
            result = tool.run({"target": "notepad.exe"}, _ctx(mock_config))

        assert result.success is False
        browser.assert_not_called()

    def test_a_directory_is_never_launched_as_a_program(self, tool, mock_config, tmp_path):
        """Directories are executable in the ``os.access`` sense; they are not apps."""
        from jarvis.tools.builtin.open_on_computer import _resolve_application

        folder = tmp_path / "SomeFolder"
        folder.mkdir()
        assert _resolve_application(str(folder)) is None


class TestPaths:
    def test_a_path_in_the_home_directory_is_opened(self, tool, mock_config, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        target = tmp_path / "notes.txt"
        target.write_text("hi", encoding="utf-8")

        with patch("jarvis.tools.builtin.open_on_computer._open_path") as opener:
            result = tool.run({"target": "notes.txt"}, _ctx(mock_config))

        assert result.success is True
        opener.assert_called_once_with(target.resolve())

    def test_a_path_outside_the_home_directory_is_refused(self, tool, mock_config, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("secret", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        with patch("jarvis.tools.builtin.open_on_computer._open_path") as opener:
            result = tool.run({"target": str(outside)}, _ctx(mock_config))

        assert result.success is False
        opener.assert_not_called()

    @pytest.mark.parametrize("suffix", [
        # ".com" is deliberately excluded here: it stays a valid top-level
        # domain for the bare-domain fallback (pre-existing, unrelated to
        # this fix), so an unopenable "invoice.com" safely falls through
        # to a harmless https://invoice.com browser open rather than
        # failing outright - covered separately below.
        "bat", "cmd", "ps1", "vbs", "vbe", "wsf", "hta", "scr", "pif",
        "js", "jse", "lnk", "py", "pyw", "sh", "reg", "msi",
    ])
    def test_an_executable_type_path_is_refused_not_run(
        self, tool, mock_config, tmp_path, monkeypatch, suffix,
    ):
        """os.startfile runs these instead of viewing them - opening one
        via this tool is indistinguishable from executing arbitrary code
        a prompt injection pointed it at (e.g. from a web search result
        suggesting a Downloads file by name)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        target = tmp_path / f"invoice.{suffix}"
        target.write_text("", encoding="utf-8")

        with patch("jarvis.tools.builtin.open_on_computer._open_path") as opener, \
             patch("jarvis.tools.builtin.open_on_computer._resolve_application",
                   return_value=None), \
             patch("jarvis.tools.builtin.open_on_computer.webbrowser.open") as browser:
            result = tool.run({"target": f"invoice.{suffix}"}, _ctx(mock_config))

        assert result.success is False
        opener.assert_not_called()
        browser.assert_not_called()

    def test_a_dot_com_named_file_falls_through_to_the_domain_not_the_opener(
        self, tool, mock_config, tmp_path, monkeypatch,
    ):
        """.com stays a valid top-level domain (pre-existing, unrelated to
        this fix): an unopenable "invoice.com" resolves to a harmless
        browser open at https://invoice.com rather than running the local
        file or failing outright."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        target = tmp_path / "invoice.com"
        target.write_text("", encoding="utf-8")

        with patch("jarvis.tools.builtin.open_on_computer._open_path") as opener, \
             patch("jarvis.tools.builtin.open_on_computer._resolve_application",
                   return_value=None), \
             patch("jarvis.tools.builtin.open_on_computer.webbrowser.open",
                   return_value=True) as browser:
            result = tool.run({"target": "invoice.com"}, _ctx(mock_config))

        assert result.success is True
        opener.assert_not_called()
        browser.assert_called_once_with("https://invoice.com")

    def test_a_plain_text_file_in_home_is_still_opened(
        self, tool, mock_config, tmp_path, monkeypatch,
    ):
        """The suffix block is specific to executable types; ordinary
        documents keep working exactly as before."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        target = tmp_path / "notes.txt"
        target.write_text("hi", encoding="utf-8")

        with patch("jarvis.tools.builtin.open_on_computer._open_path") as opener:
            result = tool.run({"target": "notes.txt"}, _ctx(mock_config))

        assert result.success is True
        opener.assert_called_once_with(target.resolve())


class TestSchemes:
    @pytest.mark.parametrize("target", [
        "file:///C:/Windows/System32/config/SAM",
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "smb://server/share",
    ])
    def test_only_http_and_https_are_opened(self, tool, mock_config, target):
        with patch("jarvis.tools.builtin.open_on_computer.webbrowser.open") as browser, \
             patch("jarvis.tools.builtin.open_on_computer.subprocess.Popen") as popen:
            result = tool.run({"target": target}, _ctx(mock_config))

        assert result.success is False
        browser.assert_not_called()
        popen.assert_not_called()

    def test_plain_http_is_allowed(self, tool, mock_config):
        with patch("jarvis.tools.builtin.open_on_computer.webbrowser.open",
                   return_value=True) as browser:
            result = tool.run({"target": "http://192.168.178.113:5000"}, _ctx(mock_config))

        assert result.success is True
        browser.assert_called_once_with("http://192.168.178.113:5000")


class TestArguments:
    @pytest.mark.parametrize("args", [None, {}, {"target": ""}, {"target": "   "}])
    def test_a_missing_target_is_refused_without_side_effects(self, tool, mock_config, args):
        with patch("jarvis.tools.builtin.open_on_computer.webbrowser.open") as browser, \
             patch("jarvis.tools.builtin.open_on_computer.subprocess.Popen") as popen:
            result = tool.run(args, _ctx(mock_config))

        assert result.success is False
        browser.assert_not_called()
        popen.assert_not_called()


class TestRegistration:
    def test_the_tool_is_available_to_the_assistant(self):
        from jarvis.tools.registry import BUILTIN_TOOLS

        assert BUILTIN_TOOLS["openOnComputer"].name == "openOnComputer"

    def test_the_schema_asks_for_exactly_one_target(self, tool):
        schema = tool.inputSchema

        assert schema["required"] == ["target"]
        assert set(schema["properties"]) == {"target"}
