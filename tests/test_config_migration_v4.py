"""Behaviour tests for the v4 config migration and the ``execution_mode`` flag.

The v4 migration is the safety net for the subscription rebuild: it takes a
backup, relocates the local-LLM connection settings into ``_legacy_local_llm``
and introduces the provider-mode keys. While ``execution_mode`` is ``local``
the relocated settings are read back, so the migration must not change a
single observable behaviour.
"""

import json

import pytest

from jarvis.config import (
    DEFAULT_CHAT_MODEL,
    LEGACY_LOCAL_LLM_KEY,
    LEGACY_LOCAL_LLM_KEYS,
    get_default_config,
    load_config,
    load_settings,
)


pytestmark = pytest.mark.unit


def _write_config(tmp_path, monkeypatch, payload):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return cfg_path


V3_CONFIG = {
    "_config_version": 3,
    "llm_provider": "ollama",
    "ollama_base_url": "http://127.0.0.1:9999",
    "ollama_chat_model": "gpt-oss:20b",
    "ollama_embed_model": "nomic-embed-text",
    "fast_model": "qwen3.5:0.8b",
    "wake_word": "friday",
}


class TestBackup:
    def test_backup_holds_the_pre_migration_config(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, monkeypatch, V3_CONFIG)

        load_config()

        backup = cfg_path.with_name(cfg_path.name + ".pre-v4.bak")
        assert backup.exists()
        assert json.loads(backup.read_text(encoding="utf-8")) == V3_CONFIG

    def test_existing_backup_is_never_overwritten(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, monkeypatch, V3_CONFIG)
        backup = cfg_path.with_name(cfg_path.name + ".pre-v4.bak")
        backup.write_text('{"kept": true}', encoding="utf-8")

        load_config()

        assert json.loads(backup.read_text(encoding="utf-8")) == {"kept": True}

    def test_no_backup_for_a_config_already_at_v4(self, tmp_path, monkeypatch):
        cfg_path = _write_config(
            tmp_path, monkeypatch, {"_config_version": 4, "wake_word": "friday"}
        )

        load_config()

        assert not cfg_path.with_name(cfg_path.name + ".pre-v4.bak").exists()


class TestRelocation:
    def test_local_llm_keys_move_into_the_legacy_block(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, monkeypatch, V3_CONFIG)

        load_config()

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["_config_version"] == 4
        legacy = on_disk[LEGACY_LOCAL_LLM_KEY]
        for key in LEGACY_LOCAL_LLM_KEYS:
            assert key not in on_disk, f"{key} should no longer sit at the top level"
        assert legacy["ollama_chat_model"] == "gpt-oss:20b"
        assert legacy["ollama_base_url"] == "http://127.0.0.1:9999"
        assert legacy["fast_model"] == "qwen3.5:0.8b"

    def test_unrelated_keys_stay_at_the_top_level(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, monkeypatch, V3_CONFIG)

        load_config()

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["wake_word"] == "friday"

    def test_nothing_is_lost(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, monkeypatch, V3_CONFIG)

        load_config()

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        recovered = {**on_disk.pop(LEGACY_LOCAL_LLM_KEY), **on_disk}
        for key, value in V3_CONFIG.items():
            if key == "_config_version":
                continue
            assert recovered[key] == value


class TestBehaviourIsUnchanged:
    def test_settings_match_the_pre_migration_settings(self, tmp_path, monkeypatch):
        _write_config(tmp_path, monkeypatch, V3_CONFIG)

        before = load_settings()  # migrates on the way through
        after = load_settings()  # reads the migrated file

        assert before == after
        assert after.ollama_chat_model == "gpt-oss:20b"
        assert after.llm_chat_model == "gpt-oss:20b"
        assert after.ollama_base_url == "http://127.0.0.1:9999"
        assert after.llm_base_url == "http://127.0.0.1:9999"
        assert after.fast_model == "qwen3.5:0.8b"
        assert after.llm_provider == "ollama"

    def test_a_top_level_key_still_wins_over_the_legacy_block(self, tmp_path, monkeypatch):
        _write_config(
            tmp_path,
            monkeypatch,
            {
                "_config_version": 4,
                "execution_mode": "local",
                "ollama_chat_model": "gemma4:e4b",
                LEGACY_LOCAL_LLM_KEY: {"ollama_chat_model": "gpt-oss:20b"},
            },
        )

        assert load_settings().ollama_chat_model == "gemma4:e4b"

    def test_subscription_mode_ignores_the_legacy_block(self, tmp_path, monkeypatch):
        _write_config(
            tmp_path,
            monkeypatch,
            {
                "_config_version": 4,
                "execution_mode": "subscription",
                LEGACY_LOCAL_LLM_KEY: {"ollama_chat_model": "gpt-oss:20b"},
            },
        )

        assert load_settings().ollama_chat_model == DEFAULT_CHAT_MODEL


class TestProviderModeKeys:
    def test_defaults_carry_the_provider_mode_keys(self):
        defaults = get_default_config()
        assert defaults["execution_mode"] == "local"
        assert defaults["default_provider"] == ""
        assert defaults["memory_provider"] == ""
        assert defaults["provider_models"] == {}
        assert defaults["stt_route_preference"] == "local_first"
        assert defaults["tts_route_preference"] == "local_first"
        assert defaults["capability_profile"] == "read_only"

    def test_migration_writes_the_provider_mode_keys(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, monkeypatch, V3_CONFIG)

        load_config()

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["execution_mode"] == "local"
        assert on_disk["default_provider"] == ""
        assert on_disk["memory_provider"] == ""
        assert on_disk["provider_models"] == {}
        assert on_disk["stt_route_preference"] == "local_first"
        assert on_disk["tts_route_preference"] == "local_first"
        assert on_disk["capability_profile"] == "read_only"

    def test_an_existing_choice_survives_the_migration(self, tmp_path, monkeypatch):
        cfg_path = _write_config(
            tmp_path, monkeypatch, {**V3_CONFIG, "capability_profile": "project_dev"}
        )

        load_config()

        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["capability_profile"] == "project_dev"

    @pytest.mark.parametrize(
        "mode,expected", [("local", "local"), ("subscription", "subscription"), ("nonsense", "local")]
    )
    def test_execution_mode_reaches_settings(self, tmp_path, monkeypatch, mode, expected):
        _write_config(
            tmp_path, monkeypatch, {"_config_version": 4, "execution_mode": mode}
        )

        assert load_settings().execution_mode == expected

    def test_route_preferences_and_profile_reach_settings(self, tmp_path, monkeypatch):
        _write_config(
            tmp_path,
            monkeypatch,
            {
                "_config_version": 4,
                "default_provider": "hermes",
                "memory_provider": "hermes",
                "provider_models": {"claude": "some-model"},
                "stt_route_preference": "cloud_first",
                "tts_route_preference": "cloud_first",
                "capability_profile": "automation",
            },
        )

        settings = load_settings()
        assert settings.default_provider == "hermes"
        assert settings.memory_provider == "hermes"
        assert settings.provider_models == {"claude": "some-model"}
        assert settings.stt_route_preference == "cloud_first"
        assert settings.tts_route_preference == "cloud_first"
        assert settings.capability_profile == "automation"

    def test_a_missing_config_file_still_runs_in_local_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "absent.json"))

        assert load_settings().execution_mode == "local"
