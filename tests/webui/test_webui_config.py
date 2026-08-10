"""Behaviour tests for the control centre's configuration keys.

Every key is checked through a real config file and ``load_settings()``. A
key that only exists in the defaults dictionary, or only on the dataclass,
ships dead: the value never reaches the code that reads it. Stub configs
hide that, so nothing here uses one.
"""

import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.config_metadata import FIELD_METADATA


WEBUI_KEYS = (
    "webui_enabled",
    "webui_port",
    "webui_bind_host",
    "webui_token",
    "webui_open_browser",
)


def _load_with(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


class TestDefaults:
    def test_control_centre_is_on_and_local_by_default(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {})

        assert cfg.webui_enabled is True
        assert cfg.webui_port == 5055
        assert cfg.webui_bind_host == "127.0.0.1"
        assert cfg.webui_token == ""
        assert cfg.webui_open_browser is False

    @pytest.mark.parametrize("key", WEBUI_KEYS)
    def test_every_key_has_a_default(self, key):
        assert key in get_default_config()

    @pytest.mark.parametrize("key", WEBUI_KEYS)
    def test_every_key_is_reachable_from_settings(self, tmp_path, monkeypatch, key):
        """Guards steps 1 to 3 of the config wiring: field, parse, construct."""
        cfg = _load_with(tmp_path, monkeypatch, {})

        assert hasattr(cfg, key)

    @pytest.mark.parametrize("key", WEBUI_KEYS)
    def test_every_key_is_editable_in_the_settings_ui(self, key):
        """Guards step 4: a key with no metadata cannot be changed by a user."""
        assert key in {meta.key for meta in FIELD_METADATA}


class TestRealConfigFile:
    def test_a_written_config_reaches_every_field(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "webui_enabled": False,
            "webui_port": 7788,
            "webui_bind_host": "0.0.0.0",
            "webui_token": "s3cret",
            "webui_open_browser": True,
        })

        assert cfg.webui_enabled is False
        assert cfg.webui_port == 7788
        assert cfg.webui_bind_host == "0.0.0.0"
        assert cfg.webui_token == "s3cret"
        assert cfg.webui_open_browser is True

    @pytest.mark.parametrize("written,expected", [
        (80, 5055),         # privileged ports need rights the app does not have
        (0, 5055),
        (70000, 5055),
        ("6060", 6060),     # a hand-edited config often quotes numbers
    ])
    def test_an_unusable_port_falls_back_to_the_default(
        self, tmp_path, monkeypatch, written, expected
    ):
        cfg = _load_with(tmp_path, monkeypatch, {"webui_port": written})

        assert cfg.webui_port == expected

    @pytest.mark.parametrize("written,expected", [
        (" 127.0.0.1 ", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("0.0.0.0", "0.0.0.0"),
    ])
    def test_bind_host_is_tidied_and_never_empty(
        self, tmp_path, monkeypatch, written, expected
    ):
        cfg = _load_with(tmp_path, monkeypatch, {"webui_bind_host": written})

        assert cfg.webui_bind_host == expected

    def test_a_token_keeps_its_exact_value_apart_from_padding(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {"webui_token": "  abc DEF  "})

        assert cfg.webui_token == "abc DEF"
