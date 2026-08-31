"""
Tests for settings window config I/O logic.

Tests verify device enumeration and save/load behaviour without touching
the GUI. Widget creation is tested via mock Qt objects where needed. The
metadata registry itself is covered by tests/test_config_metadata.py.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from desktop_app.settings_window import (
    get_input_devices,
    _MCPCatalogueDialog,
    _MCPEditDialog,
)
from desktop_app.mcp_catalogue import CATALOGUE_BY_NAME
from jarvis.config import get_default_config
from jarvis.config_metadata import FIELD_METADATA, _is_default_value


class TestInputDevices:
    """Tests for audio device enumeration."""

    def test_always_includes_system_default(self):
        """get_input_devices() always returns at least the system default."""
        # Even if sounddevice fails, we should get the default option
        with patch.dict("sys.modules", {"sounddevice": None}):
            devices = get_input_devices()
        assert len(devices) >= 1
        assert devices[0][0] == ""  # empty string = system default

    def test_with_mock_sounddevice(self):
        """With mock devices, returns them plus system default."""
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 2, "default_samplerate": 44100},
            {"name": "USB Speaker", "max_input_channels": 0, "default_samplerate": 48000},
            {"name": "External Mic", "max_input_channels": 1, "default_samplerate": 16000},
        ]
        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            # Need to reimport to pick up the mock
            import importlib
            import desktop_app.settings_window as sw
            importlib.reload(sw)
            devices = sw.get_input_devices()

        # System default + 2 input devices (USB Speaker has 0 input channels)
        assert len(devices) == 3
        assert devices[0][0] == ""
        assert "Built-in Mic" in devices[1][1]
        assert "External Mic" in devices[2][1]

    def test_handles_sounddevice_import_error(self):
        """Gracefully handles missing sounddevice."""
        devices = get_input_devices()
        # Should always at least have the default
        assert len(devices) >= 1


class TestConfigSaveLogic:
    """Tests for save/load round-trip behaviour."""

    def test_only_non_defaults_are_saved(self):
        """Saving default values should produce an empty config file."""
        defaults = get_default_config()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{}')
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            # Simulate: all values match defaults, so nothing should be written
            config = {}
            for fm in FIELD_METADATA:
                val = defaults.get(fm.key)
                default_val = defaults.get(fm.key)
                if val != default_val:
                    config[fm.key] = val

            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert saved == {}
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_changed_values_are_preserved(self):
        """Non-default values should survive a save/load round-trip."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{}')
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            config = {
                "ollama_chat_model": "gemma4:e4b",
                "tts_enabled": False,
                "hot_window_seconds": 5.0,
            }
            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert saved["ollama_chat_model"] == "gemma4:e4b"
            assert saved["tts_enabled"] is False
            assert saved["hot_window_seconds"] == 5.0
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_unknown_keys_preserved_on_save(self):
        """Keys not in FIELD_METADATA (e.g. mcps) should survive save."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"mcps": {"test": {"url": "http://example.com"}},
                        "_config_version": 1}, f)
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            existing = _load_json(cfg_path)
            # Simulate settings save: add a changed value, keep existing keys
            existing["tts_enabled"] = False
            _save_json(cfg_path, existing)

            saved = _load_json(cfg_path)
            assert "mcps" in saved
            assert saved["mcps"]["test"]["url"] == "http://example.com"
            assert saved["_config_version"] == 1
            assert saved["tts_enabled"] is False
        finally:
            cfg_path.unlink(missing_ok=True)


class TestMCPEditDialogLogic:
    """Tests for the MCP edit dialog's get_result() logic (no GUI)."""

    def test_get_result_basic(self):
        """get_result parses name, command, args, and env correctly."""
        dlg = _MCPEditDialog.__new__(_MCPEditDialog)
        dlg._name_edit = MagicMock()
        dlg._name_edit.text.return_value = "test-server"
        dlg._command_edit = MagicMock()
        dlg._command_edit.text.return_value = "npx"
        dlg._args_edit = MagicMock()
        dlg._args_edit.text.return_value = "-y @test/server ~"
        dlg._env_edit = MagicMock()
        dlg._env_edit.text.return_value = "API_KEY=abc123"

        name, cfg = dlg.get_result()
        assert name == "test-server"
        assert cfg["transport"] == "stdio"
        assert cfg["command"] == "npx"
        assert cfg["args"] == ["-y", "@test/server", "~"]
        assert cfg["env"] == {"API_KEY": "abc123"}

    def test_get_result_empty_env(self):
        """When env is empty, env key should not be in config."""
        dlg = _MCPEditDialog.__new__(_MCPEditDialog)
        dlg._name_edit = MagicMock()
        dlg._name_edit.text.return_value = "test"
        dlg._command_edit = MagicMock()
        dlg._command_edit.text.return_value = "node"
        dlg._args_edit = MagicMock()
        dlg._args_edit.text.return_value = ""
        dlg._env_edit = MagicMock()
        dlg._env_edit.text.return_value = ""

        name, cfg = dlg.get_result()
        assert name == "test"
        assert cfg["command"] == "node"
        assert cfg["args"] == []
        assert "env" not in cfg

    def test_get_result_multiple_env_vars(self):
        """Multiple KEY=VALUE pairs are parsed correctly."""
        dlg = _MCPEditDialog.__new__(_MCPEditDialog)
        dlg._name_edit = MagicMock()
        dlg._name_edit.text.return_value = "srv"
        dlg._command_edit = MagicMock()
        dlg._command_edit.text.return_value = "cmd"
        dlg._args_edit = MagicMock()
        dlg._args_edit.text.return_value = ""
        dlg._env_edit = MagicMock()
        dlg._env_edit.text.return_value = "A=1 B=two C=three=four"

        _, cfg = dlg.get_result()
        assert cfg["env"] == {"A": "1", "B": "two", "C": "three=four"}


class TestMCPCatalogueDialogLogic:
    """Tests for the MCP catalogue dialog's Node.js detection (no GUI)."""

    def test_is_node_available_returns_true_when_found(self):
        """_is_node_available returns True when _resolve_command succeeds."""
        with patch("jarvis.tools.external.mcp_client._resolve_command", return_value="/usr/bin/npx"):
            assert _MCPCatalogueDialog._is_node_available() is True

    def test_is_node_available_returns_false_when_missing(self):
        """_is_node_available returns False when _resolve_command raises."""
        with patch("jarvis.tools.external.mcp_client._resolve_command", side_effect=FileNotFoundError("not found")):
            assert _MCPCatalogueDialog._is_node_available() is False


class TestMCPConfigSaveLogic:
    """Tests for MCP config preservation during save."""

    def test_mcps_saved_when_present(self):
        """MCP configs should be written to the config file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            config = {
                "mcps": {
                    "filesystem": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "~"],
                    }
                }
            }
            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert "mcps" in saved
            assert "filesystem" in saved["mcps"]
            assert saved["mcps"]["filesystem"]["command"] == "npx"
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_empty_mcps_not_saved(self):
        """When mcps is empty, it should not be written to config."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            # Simulate: mcps is empty so should not be written
            config = {"tts_enabled": False}
            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert "mcps" not in saved
        finally:
            cfg_path.unlink(missing_ok=True)


class TestChoiceFieldsShowWhatIsConfigured:
    """A select must be able to display the value the file actually holds.

    The supported-model list is a curated shortlist, not the set of models a
    local runtime can serve: a tag built from a custom Modelfile will never
    appear in it. A combo box that cannot show the configured model silently
    displays a different one, and this window reads every widget back on
    save, so the wrong model then gets written to the file.
    """

    def _window(self, tmp_path, monkeypatch, qapp, stored):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(stored), encoding="utf-8")
        monkeypatch.setattr(
            "desktop_app.settings_window.default_config_path", lambda: config_path
        )
        from desktop_app.settings_window import SettingsWindow

        return SettingsWindow()

    def test_a_model_the_registry_never_heard_of_survives_a_save(
        self, tmp_path, monkeypatch, qapp
    ):
        window = self._window(
            tmp_path, monkeypatch, qapp, {"ollama_chat_model": "qwen2.5:7b-ctx8k"}
        )
        meta = next(m for m in FIELD_METADATA if m.key == "ollama_chat_model")

        assert window._get_value(meta) == "qwen2.5:7b-ctx8k"

    def test_a_known_model_is_offered_once(self, tmp_path, monkeypatch, qapp):
        from jarvis.config_metadata import SUPPORTED_CHAT_MODELS

        known = next(iter(SUPPORTED_CHAT_MODELS))
        window = self._window(tmp_path, monkeypatch, qapp, {"ollama_chat_model": known})
        combo = window._widgets["ollama_chat_model"]

        offered = [combo.itemData(i) for i in range(combo.count())]

        assert offered.count(known) == 1


class TestSettingsPipelineLayout:
    def test_qt_pages_render_metadata_section_headings(
        self, tmp_path, monkeypatch, qapp,
    ):
        from PyQt6.QtWidgets import QLabel
        from desktop_app.settings_window import SettingsWindow

        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "desktop_app.settings_window.default_config_path", lambda: config_path
        )
        window = SettingsWindow()

        sidebar = [window._sidebar.item(i).text() for i in range(window._sidebar.count())]
        assert any("Local AI & Behaviour" in label for label in sidebar)
        assert any("Speech Recognition" in label for label in sidebar)
        assert any("Speech Output" in label for label in sidebar)
        assert not any("Piper TTS" in label for label in sidebar)

        sections = {
            label.text()
            for label in window.findChildren(QLabel)
            if label.objectName() == "settingsSection"
        }
        assert {
            "Local models", "Timeouts", "Thinking and behaviour", "Microphone",
            "Wake word", "Voice activity and endpointing", "Whisper",
            "Common output", "Cloud chain", "Piper", "Chatterbox", "Kokoro",
        } <= sections


class TestCloudTTSProviderEditor:
    def _window(self, tmp_path, monkeypatch, qapp, providers):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "_config_version": 5,
            "tts_cloud_providers": providers,
        }), encoding="utf-8")
        monkeypatch.setattr(
            "desktop_app.settings_window.default_config_path", lambda: config_path
        )
        from desktop_app.settings_window import SettingsWindow

        return SettingsWindow()

    @staticmethod
    def _providers():
        return [
            {
                "name": "Fish", "provider": "fish_audio",
                "api_key_env": "FISH_AUDIO_API_KEY", "voice_id": "fish-voice",
                "model": "s2.1-pro-free", "enabled": True, "timeout_sec": 7.0,
            },
            {
                "name": "Eleven", "provider": "elevenlabs",
                "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "eleven-voice",
                "model": "eleven_multilingual_v2", "enabled": False,
                "timeout_sec": 8.5,
            },
        ]

    def test_qt_editor_preserves_all_fields_and_order(
        self, tmp_path, monkeypatch, qapp,
    ):
        window = self._window(tmp_path, monkeypatch, qapp, self._providers())
        meta = next(field for field in FIELD_METADATA if field.key == "tts_cloud_providers")

        assert window._get_value(meta) == self._providers()

    def test_qt_editor_can_disable_and_reorder_providers(
        self, tmp_path, monkeypatch, qapp,
    ):
        window = self._window(tmp_path, monkeypatch, qapp, self._providers())
        editor = window._widgets["tts_cloud_providers"]
        table = editor._table_widget
        meta = next(field for field in FIELD_METADATA if field.key == "tts_cloud_providers")
        enabled_column = next(
            index for index, field in enumerate(editor._item_fields)
            if field.key == "enabled"
        )
        table.cellWidget(0, enabled_column).setChecked(False)
        table.selectRow(0)
        editor._move_down_button.click()

        saved = window._get_value(meta)
        assert [provider["name"] for provider in saved] == ["Eleven", "Fish"]
        assert saved[1]["enabled"] is False

    def test_qt_editor_does_not_read_a_credential_from_the_environment(
        self, tmp_path, monkeypatch, qapp,
    ):
        monkeypatch.setenv("FISH_AUDIO_API_KEY", "must-never-reach-qt")
        window = self._window(tmp_path, monkeypatch, qapp, self._providers())
        editor = window._widgets["tts_cloud_providers"]

        visible_text = []
        for row in range(editor._table_widget.rowCount()):
            for column in range(editor._table_widget.columnCount()):
                item = editor._table_widget.item(row, column)
                if item is not None:
                    visible_text.append(item.text())

        assert "FISH_AUDIO_API_KEY" in visible_text
        assert "must-never-reach-qt" not in visible_text
