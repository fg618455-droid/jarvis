"""Migration into the single tiered LLM route schema."""

from __future__ import annotations

import json

from jarvis.config import _migrate_config


def _migrate(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    migrated = _migrate_config(path, dict(config))
    return migrated, json.loads(path.read_text(encoding="utf-8"))


def test_v5_keeps_tiered_routes_and_adds_safe_defaults(tmp_path):
    route = {
        "name": "primary",
        "provider": "openai_compatible",
        "base_url": "https://example.test/v1",
        "api_key": "secret",
        "model": "chat-model",
        "tier": "chat",
        "timeout_sec": 4.0,
    }

    migrated, stored = _migrate(tmp_path, {
        "_config_version": 4,
        "llm_routes": [route],
    })

    assert migrated["_config_version"] == 5
    assert migrated["llm_routes"] == [{
        **route,
        "api_key_env": "",
        "enabled": True,
        "capabilities": ["chat", "stream", "tools"],
    }]
    assert stored == migrated


def test_v5_converts_priority_routes_into_ordered_tier_entries(tmp_path):
    migrated, _stored = _migrate(tmp_path, {
        "_config_version": 4,
        "ollama_base_url": "http://127.0.0.1:11434",
        "llm_routes": [
            {
                "id": "local",
                "provider": "ollama",
                "model": "local-model",
                "priority": 0,
                "enabled": True,
                "local": True,
                "capabilities": ["chat", "stream", "tools"],
            },
            {
                "id": "cloud",
                "provider": "openai_compatible",
                "base_url": "https://cloud.test/v1",
                "model": "cloud-model",
                "priority": 10,
                "enabled": True,
                "api_key_env": "CLOUD_KEY",
                "capabilities": ["chat", "stream"],
            },
        ],
    })

    routes = migrated["llm_routes"]
    assert [(route["name"], route["tier"]) for route in routes] == [
        ("local", "fast"),
        ("cloud", "fast"),
        ("local", "chat"),
        ("cloud", "chat"),
    ]
    assert routes[0]["base_url"] == "http://127.0.0.1:11434"
    assert routes[1]["api_key_env"] == "CLOUD_KEY"
    assert routes[1]["api_key"] == ""
    assert "priority" not in routes[1]
    assert "local" not in routes[0]


def test_v5_preserves_disabled_routes_without_reading_their_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_KEY", "must-not-be-copied")
    migrated, _stored = _migrate(tmp_path, {
        "_config_version": 4,
        "llm_routes": [{
            "id": "backup",
            "provider": "openai_compatible",
            "base_url": "https://backup.test/v1",
            "model": "backup-model",
            "priority": 30,
            "enabled": False,
            "api_key_env": "BACKUP_KEY",
        }],
    })

    assert len(migrated["llm_routes"]) == 2
    assert all(route["enabled"] is False for route in migrated["llm_routes"])
    assert all(route["api_key_env"] == "BACKUP_KEY" for route in migrated["llm_routes"])
    assert "must-not-be-copied" not in json.dumps(migrated)


def test_v5_migration_is_idempotent(tmp_path):
    initial = {
        "_config_version": 4,
        "llm_routes": [{
            "id": "cloud",
            "provider": "openai_compatible",
            "base_url": "https://cloud.test/v1",
            "model": "model",
            "enabled": True,
        }],
    }
    first, _stored = _migrate(tmp_path, initial)
    second, _stored_again = _migrate(tmp_path, first)
    assert second == first
