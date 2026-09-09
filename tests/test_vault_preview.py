from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


def test_preview_prints_emoji_plan_without_touching_synthetic_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    private = vault / "private.md"
    private.write_text("private note", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "db_path": str(tmp_path / "jarvis.db"),
                "obsidian_vault_path": str(vault),
                "obsidian_memory_folder": "Jarvis",
                "obsidian_write_mode": "dry_run",
            }
        ),
        encoding="utf-8",
    )
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    env = os.environ.copy()
    env["JARVIS_CONFIG_PATH"] = str(config)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [sys.executable, "-m", "jarvis.memory.vault.preview"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    after = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    assert result.returncode == 0, result.stderr
    assert "🗂️ Vault mirror plan" in result.stdout
    assert "➕ create" in result.stdout
    assert after == before
