"""Behaviour tests for keeping a remote-only chain remote-only.

A chain built entirely from remote routes gains a loopback Ollama entry for
FAST and CHAT by default, so a turn still answers when every remote provider
is down. Someone running deliberately remote-only does not want that: waking a
local model costs VRAM and answers in a voice the remote chain would never
have produced. The switch turns that safety net off.

PRIVATE work and embeddings are outside the switch. They are local because the
data is private, not because the chain happens to lack a remote option, so
they stay local in both settings.
"""

import json

from jarvis.config import load_settings
from jarvis.llm import Tier, get_llm_backend
from jarvis.llm.factory import describe_model_topology


REMOTE_ROUTE = {
    "name": "remote-chat",
    "provider": "openai_compatible",
    "base_url": "http://example.invalid/v1",
    "api_key": "k",
    "model": "remote-model",
    "tier": "chat",
    "timeout_sec": 5.0,
    "enabled": True,
    "capabilities": ["chat", "tools"],
}


def _load_with(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


def _route_names(cfg):
    backend = get_llm_backend(cfg)
    return [route.name for route in backend._routes]


class TestLocalFallbackSwitch:
    def test_default_keeps_the_local_safety_net(self, tmp_path, monkeypatch):
        """Existing setups keep answering when the remote chain fails."""
        cfg = _load_with(tmp_path, monkeypatch, {"llm_routes": [REMOTE_ROUTE]})

        assert cfg.local_llm_fallback_enabled is True
        assert "local-chat" in _route_names(cfg)

    def test_switching_off_drops_the_local_chat_route(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "llm_routes": [REMOTE_ROUTE],
            "local_llm_fallback_enabled": False,
        })

        names = _route_names(cfg)
        assert "local-chat" not in names
        assert "local-fast" not in names

    def test_private_stays_local_either_way(self, tmp_path, monkeypatch):
        """Memory writes never leave the machine, switch or no switch."""
        cfg = _load_with(tmp_path, monkeypatch, {
            "llm_routes": [REMOTE_ROUTE],
            "local_llm_fallback_enabled": False,
        })

        backend = get_llm_backend(cfg)
        private = [r for r in backend._routes if r.tier is Tier.PRIVATE]
        assert [r.name for r in private] == ["local-private"]


class TestReportedTopology:
    def test_off_reports_no_local_chat_model(self, tmp_path, monkeypatch):
        """The UI must not advertise a local chat model that cannot run."""
        cfg = _load_with(tmp_path, monkeypatch, {
            "llm_routes": [REMOTE_ROUTE],
            "local_llm_fallback_enabled": False,
        })

        local = describe_model_topology(cfg)["local"]
        assert "chat_fallback" not in local
        assert "fast_fallback" not in local
        assert local["private"]["provider"] == "ollama"
        assert local["embedding"]["provider"] == "ollama"

    def test_on_reports_the_fallbacks(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {"llm_routes": [REMOTE_ROUTE]})

        local = describe_model_topology(cfg)["local"]
        assert "chat_fallback" in local
        assert "fast_fallback" in local
