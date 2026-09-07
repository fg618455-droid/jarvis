"""Ollama serves memory and embeddings, never a reply.

FAST and CHAT are answered by configured routes alone. A local model is
never appended to either chain, never offered as a route protocol, and
never named as a pinnable chat backend, so nothing a user does in the
control centre can put one back in front of a conversation. PRIVATE work
and embeddings stay on loopback Ollama, which is a privacy invariant and
not a reply path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from jarvis.llm import Tier
from jarvis.llm.factory import describe_model_topology, get_llm_backend


STATIC = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "webui" / "static"


def _settings(routes):
    return SimpleNamespace(
        llm_routes=routes,
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="gemma4:e2b",
        ollama_embed_model="nomic-embed-text",
        llm_chat_model="cloud-chat",
        fast_model="cloud-fast",
        local_fast_model="gemma4:e2b",
        llm_provider="ollama",
    )


def _route(name, tier, provider="openai_compatible"):
    return {
        "name": name,
        "provider": provider,
        "base_url": "https://cloud.test/v1",
        "api_key": "",
        "api_key_env": "",
        "model": f"{name}-model",
        "tier": tier,
        "timeout_sec": 30.0,
        "enabled": True,
        "capabilities": ["chat", "stream", "tools"],
    }


class TestNoLocalReplyRoute:
    def test_a_remote_only_chain_gets_no_local_chat_route(self):
        backend = get_llm_backend(_settings([_route("cloud", "chat")]))

        assert [route.provider for route in backend.routes_for(Tier.CHAT)] == [
            "openai_compatible"
        ]

    def test_a_remote_only_chain_gets_no_local_fast_route(self):
        backend = get_llm_backend(_settings([_route("cloud", "fast")]))

        assert [route.provider for route in backend.routes_for(Tier.FAST)] == [
            "openai_compatible"
        ]

    def test_an_empty_chat_chain_stays_empty(self):
        """Nothing configured means nothing answers. Waking a local model to
        fill the gap is exactly what must not happen."""
        backend = get_llm_backend(_settings([_route("cloud", "fast")]))

        assert backend.routes_for(Tier.CHAT) == ()

    def test_no_configured_routes_at_all_leaves_both_reply_chains_empty(self):
        backend = get_llm_backend(_settings([]))

        assert backend.routes_for(Tier.CHAT) == ()
        assert backend.routes_for(Tier.FAST) == ()

    def test_a_route_configured_onto_ollama_is_dropped(self):
        """The protocol is gone from the editor, but a config file written
        before it went is still on disk. Such an entry is ignored rather
        than quietly promoted to answering."""
        backend = get_llm_backend(_settings([
            _route("leftover", "chat", provider="ollama"),
            _route("cloud", "chat"),
        ]))

        assert [route.name for route in backend.routes_for(Tier.CHAT)] == ["cloud"]

    def test_private_work_still_runs_on_a_local_model(self):
        backend = get_llm_backend(_settings([_route("cloud", "chat")]))
        private = backend.routes_for(Tier.PRIVATE)

        assert [route.provider for route in private] == ["ollama"]
        assert private[0].model == "gemma4:e2b"


class TestThroughTheRealConfigLoader:
    """The same guarantee from a config file rather than a hand-built
    settings double, because that is the path a running install takes."""

    @staticmethod
    def _load(tmp_path, monkeypatch, values):
        from jarvis.config import load_settings

        path = tmp_path / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        return load_settings()

    def test_a_remote_chain_gains_no_local_reply_route(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {
            "llm_routes": [_route("remote-chat", "chat")],
        })
        names = [route.name for route in get_llm_backend(cfg).routes]

        assert "local-chat" not in names
        assert "local-fast" not in names

    def test_memory_still_writes_through_a_local_model(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {
            "llm_routes": [_route("remote-chat", "chat")],
        })
        private = get_llm_backend(cfg).routes_for(Tier.PRIVATE)

        assert [route.name for route in private] == ["local-private"]

    def test_a_retired_switch_left_on_disk_changes_nothing(self, tmp_path, monkeypatch):
        """Config files in the wild still carry the old opt-out. It is not a
        setting any more, and an unknown key must not resurrect the route it
        used to control."""
        cfg = self._load(tmp_path, monkeypatch, {
            "llm_routes": [_route("remote-chat", "chat")],
            "local_llm_fallback_enabled": True,
        })
        names = [route.name for route in get_llm_backend(cfg).routes]

        assert "local-chat" not in names


class TestTopologyReport:
    def test_no_local_reply_roles_are_reported(self):
        local = describe_model_topology(_settings([_route("cloud", "chat")]))["local"]

        assert "chat_fallback" not in local
        assert "fast_fallback" not in local

    def test_private_and_embedding_roles_are_still_reported(self):
        local = describe_model_topology(_settings([_route("cloud", "chat")]))["local"]

        assert local["private"]["model"] == "gemma4:e2b"
        assert local["embedding"]["model"] == "nomic-embed-text"


class TestControlCentreOffersNoLocalChat:
    def test_ollama_is_not_a_pinnable_chat_backend(self):
        source = (STATIC / "js" / "views" / "llm.js").read_text(encoding="utf-8")
        choices = source.split("CHAT_BACKEND_CHOICES = [", 1)[1].split("]", 1)[0]

        assert '"ollama"' not in choices

    def test_ollama_is_not_a_route_protocol(self):
        from jarvis.config_metadata import LLM_ROUTE_FIELD_METADATA

        provider = next(f for f in LLM_ROUTE_FIELD_METADATA if f.key == "provider")

        assert "ollama" not in [value for value, _label in provider.choices]

    def test_the_local_model_table_shows_no_reply_roles(self):
        source = (STATIC / "js" / "views" / "system.js").read_text(encoding="utf-8")

        assert "chat_fallback" not in source
        assert "fast_fallback" not in source


class TestRouteApiRejectsOllama:
    def test_saving_an_ollama_route_is_refused(self):
        from jarvis.webui.api.llm import _normalise_routes

        try:
            _normalise_routes([_route("local", "chat", provider="ollama")], [])
        except ValueError as error:
            assert "protocol" in str(error)
        else:  # pragma: no cover - the assertion below reports the failure
            raise AssertionError("an ollama route must not be accepted")

    def test_a_route_named_after_a_retired_provider_is_refused(self):
        """The protocol list is the whole set of protocols; anything else is
        a typo or a stale config, and both deserve to be told so."""
        from jarvis.webui.api.llm import _normalise_routes

        try:
            _normalise_routes([_route("groq", "chat", provider="groq")], [])
        except ValueError as error:
            assert "protocol" in str(error)
        else:  # pragma: no cover - the assertion below reports the failure
            raise AssertionError("an unknown protocol must not be accepted")
