"""Behaviour tests for the Mission Control configuration keys.

Same shape as ``test_webui_config.py``: a key that only exists in the
defaults dictionary, or only on the dataclass, ships dead.
"""

import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.config_metadata import FIELD_METADATA


CREW_KEYS = ("crew_api_url", "crew_api_key")


def _load_with(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


class TestDefaults:
    def test_the_crew_endpoint_is_unset_by_default(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {})

        assert cfg.crew_api_url == ""
        assert cfg.crew_api_key == ""

    @pytest.mark.parametrize("key", CREW_KEYS)
    def test_every_key_has_a_default(self, key):
        assert key in get_default_config()

    @pytest.mark.parametrize("key", CREW_KEYS)
    def test_every_key_is_reachable_from_settings(self, tmp_path, monkeypatch, key):
        cfg = _load_with(tmp_path, monkeypatch, {})

        assert hasattr(cfg, key)

    @pytest.mark.parametrize("key", CREW_KEYS)
    def test_every_key_is_editable_in_the_settings_ui(self, key):
        assert key in {meta.key for meta in FIELD_METADATA}


class TestRealConfigFile:
    def test_a_written_config_reaches_every_field(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "crew_api_url": "http://192.168.178.113:8643",
            "crew_api_key": "s3cret",
        })

        assert cfg.crew_api_url == "http://192.168.178.113:8643"
        assert cfg.crew_api_key == "s3cret"

    def test_a_trailing_slash_is_dropped(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "crew_api_url": "http://192.168.178.113:8643/",
        })

        assert cfg.crew_api_url == "http://192.168.178.113:8643"

    def test_padding_is_stripped(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {"crew_api_key": "  abc DEF  "})

        assert cfg.crew_api_key == "abc DEF"
