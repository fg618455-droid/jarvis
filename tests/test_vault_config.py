from __future__ import annotations

import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.config_metadata import FIELD_METADATA


pytestmark = pytest.mark.unit


def _write_config(tmp_path, monkeypatch, values):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))


def test_safe_defaults_require_explicit_write_opt_in():
    defaults = get_default_config()
    assert defaults["obsidian_vault_path"] is None
    assert defaults["obsidian_memory_folder"] == "Jarvis"
    assert defaults["obsidian_write_mode"] == "dry_run"
    assert defaults["obsidian_read_enabled"] is True
    assert defaults["obsidian_read_max_results"] == 3
    assert defaults["obsidian_index_max_file_kb"] == 512


def test_nonexistent_vault_degrades_to_disabled_with_warning(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch, {"obsidian_vault_path": str(tmp_path / "missing")})

    cfg = load_settings()

    assert cfg.obsidian_vault_path is None
    assert "⚠️" in capsys.readouterr().out


@pytest.mark.parametrize("folder", ["", "../Private", "Jarvis/../../Private"])
def test_invalid_memory_folder_disables_mirror_without_fallback(tmp_path, monkeypatch, folder):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "obsidian_vault_path": str(vault),
            "obsidian_memory_folder": folder,
            "obsidian_write_mode": "on",
        },
    )

    cfg = load_settings()

    assert cfg.obsidian_memory_folder is None
    assert cfg.obsidian_write_mode == "off"


def test_all_vault_settings_are_registered_in_metadata():
    keys = {field.key for field in FIELD_METADATA}
    assert {
        "obsidian_vault_path",
        "obsidian_memory_folder",
        "obsidian_write_mode",
        "obsidian_read_enabled",
        "obsidian_read_max_results",
        "obsidian_index_max_file_kb",
    } <= keys
