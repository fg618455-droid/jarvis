"""Behaviour tests for the LLM factory dispatch.

Covers ``get_llm_backend`` and ``get_embedding_backend`` selection
across provider configurations: Ollama default, OpenAI-compatible,
embedding override to Ollama when chat runs on a runtime without
embeddings, and back-fill from the legacy ``ollama_*`` fields when
the new ``llm_*`` fields are unset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Cfg:
    llm_provider: str = "ollama"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_chat_model: str = ""
    embedding_provider: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_chat_model: str = "gemma4:e2b"
    ollama_embed_model: str = "nomic-embed-text"


class TestGetLLMBackend:
    def test_default_provider_has_no_chat_route_and_private_ollama(self):
        from jarvis.llm import RoutedBackend, Tier, get_llm_backend

        backend = get_llm_backend(_Cfg())

        assert isinstance(backend, RoutedBackend)
        assert backend.routes_for(Tier.CHAT) == ()
        assert [route.provider for route in backend.routes_for(Tier.PRIVATE)] == ["ollama"]

    def test_legacy_local_openai_endpoint_is_not_a_chat_route(self):
        from jarvis.llm import RoutedBackend, Tier, get_llm_backend

        cfg = _Cfg(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:1234/v1",
            llm_api_key="sk-test",
        )

        backend = get_llm_backend(cfg)

        assert isinstance(backend, RoutedBackend)
        assert backend.routes_for(Tier.CHAT) == ()

    def test_unknown_provider_does_not_fall_back_to_ollama(self):
        from jarvis.llm import Tier, get_llm_backend

        cfg = _Cfg(llm_provider="lm-studio")  # unknown alias

        backend = get_llm_backend(cfg)

        assert backend.routes_for(Tier.CHAT) == ()

    def test_private_ollama_is_forced_to_loopback(self):
        from jarvis.llm import Tier, get_llm_backend

        cfg = _Cfg(ollama_base_url="http://1.2.3.4:11434")

        backend = get_llm_backend(cfg)

        assert backend.routes_for(Tier.CHAT) == ()
        assert backend.routes_for(Tier.PRIVATE)[0].base_url == "http://127.0.0.1:11434"

    def test_ollama_provider_never_creates_chat_route(self):
        """``llm_base_url`` is the OpenAI-compatible server's URL. When the
        provider is Ollama, the backend must use ``ollama_base_url`` and
        ignore any ``llm_base_url`` left over from a previous
        OpenAI-compatible configuration — otherwise toggling the provider
        back to Ollama would silently point OllamaBackend at the old
        LM Studio URL."""
        from jarvis.llm import Tier, get_llm_backend

        cfg = _Cfg(
            llm_provider="ollama",
            llm_base_url="http://lmstudio:1234/v1",  # stale from a prior switch
            ollama_base_url="http://127.0.0.1:11434",
        )

        backend = get_llm_backend(cfg)

        assert backend.routes_for(Tier.CHAT) == ()
        assert backend.routes_for(Tier.PRIVATE)[0].base_url == "http://127.0.0.1:11434"


class TestGetEmbeddingBackend:
    def test_embeddings_stay_on_loopback_ollama(self):
        from jarvis.llm import OllamaBackend, get_embedding_backend

        cfg = _Cfg(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:1234/v1",
        )

        backend = get_embedding_backend(cfg)

        assert isinstance(backend, OllamaBackend)
        assert backend.base_url == "http://127.0.0.1:11434"

    def test_override_to_ollama_when_chat_runs_on_openai_compatible(self):
        """The override exists for runtimes that ship chat without
        embeddings (e.g. some oMLX builds). The user pins embeddings to
        Ollama; chat keeps running on the configured provider."""
        from jarvis.llm import OllamaBackend, get_embedding_backend

        cfg = _Cfg(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:1234/v1",
            embedding_provider="ollama",
        )

        backend = get_embedding_backend(cfg)

        assert isinstance(backend, OllamaBackend)
        assert backend.base_url == "http://127.0.0.1:11434"

    def test_remote_embedding_override_is_ignored(self):
        from jarvis.llm import OllamaBackend, get_embedding_backend

        cfg = _Cfg(
            llm_provider="ollama",
            embedding_provider="openai_compatible",
            embedding_base_url="http://embed-host:9000/v1",
        )

        backend = get_embedding_backend(cfg)

        assert isinstance(backend, OllamaBackend)
        assert backend.base_url == "http://127.0.0.1:11434"

    def test_does_not_inherit_chat_base_url_for_embeddings(self):
        """Most common LM Studio config: chat and embeddings on the same
        OpenAI-compatible server. With ``embedding_provider`` unset and
        ``embedding_base_url`` empty, the embedding backend must inherit
        ``llm_base_url`` rather than falling back to the Ollama URL."""
        from jarvis.llm import OllamaBackend, get_embedding_backend

        cfg = _Cfg(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:1234/v1",
        )

        backend = get_embedding_backend(cfg)

        assert isinstance(backend, OllamaBackend)
        assert backend.base_url == "http://127.0.0.1:11434"

    def test_never_copies_cloud_key_into_embedding_backend(self):
        from jarvis.llm import OllamaBackend, get_embedding_backend

        cfg = _Cfg(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:1234/v1",
            llm_api_key="sk-shared",
        )

        backend = get_embedding_backend(cfg)

        assert isinstance(backend, OllamaBackend)
        assert not hasattr(backend, "_api_key")

    def test_remote_embedding_provider_without_url_still_uses_local_ollama(self):
        """When the user picks openai_compatible but provides no URL on
        any of llm_base_url, ollama_base_url, embedding_base_url, the
        factory falls back to the Ollama default rather than raising.
        Construction is fail-soft; the request will fail at call time."""
        from jarvis.llm import OllamaBackend, get_embedding_backend

        cfg = _Cfg(
            llm_provider="ollama",
            embedding_provider="openai_compatible",
        )

        backend = get_embedding_backend(cfg)

        assert isinstance(backend, OllamaBackend)
        assert backend.base_url == "http://127.0.0.1:11434"


class TestPerProviderModelResolution:
    """``cfg.llm_chat_model`` / ``cfg.embedding_model`` resolve per-provider:
    the Ollama models are authoritative on the Ollama path, the
    OpenAI-compatible models on that path. This mirrors the factory's
    per-provider base-URL resolution and stops a stale ``llm_chat_model``
    (e.g. promoted by the v2 migration) from shadowing the Ollama model
    picker, which writes ``ollama_chat_model``."""

    def _load(self, tmp_path, monkeypatch, cfg: dict):
        import json
        cfg_path = tmp_path / "config.json"
        cfg.setdefault("_config_version", 2)  # skip migration noise
        cfg_path.write_text(json.dumps(cfg))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        from jarvis.config import load_settings
        return load_settings()

    def test_chat_model_is_not_rewritten_from_private_ollama_model(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "llm_provider": "ollama",
            "ollama_chat_model": "new-pick:7b",
            "llm_chat_model": "stale-migrated:70b",
        })
        assert settings.llm_chat_model == "stale-migrated:70b"

    def test_openai_compatible_uses_llm_chat_model(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "llm_provider": "openai_compatible",
            "llm_base_url": "http://localhost:1234/v1",
            "llm_chat_model": "lmstudio/gemma",
            "ollama_chat_model": "gemma4:e2b",
        })
        assert settings.llm_chat_model == "lmstudio/gemma"

    def test_openai_compatible_never_borrows_private_ollama_model(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "llm_provider": "openai_compatible",
            "llm_base_url": "http://localhost:1234/v1",
            "ollama_chat_model": "gemma4:e2b",
        })
        assert settings.llm_chat_model == ""

    def test_embedding_model_not_shadowed_on_ollama_path(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "llm_provider": "ollama",
            "ollama_embed_model": "nomic-embed-text",
            "embedding_model": "stale-openai-embed",
        })
        assert settings.embedding_model == "nomic-embed-text"

    def test_embedding_provider_is_migrated_back_to_local_ollama(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, {
            "llm_provider": "ollama",
            "embedding_provider": "openai_compatible",
            "embedding_base_url": "http://embed:9000/v1",
            "embedding_model": "text-embedding-3-small",
            "ollama_embed_model": "nomic-embed-text",
        })
        assert settings.embedding_model == "nomic-embed-text"
        assert settings.embedding_provider == "ollama"


class TestConfigMigration:
    def test_v1_config_promotes_ollama_fields_to_new_keys(self, tmp_path, monkeypatch):
        """A pre-PR-2 (v1) config on disk should auto-fill the new
        ``llm_*`` and ``embedding_*`` keys from the matching
        ``ollama_*`` ones so existing installs keep working."""
        import json

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "_config_version": 1,
                    "ollama_base_url": "http://1.2.3.4:11434",
                    "ollama_chat_model": "my-model",
                    "ollama_embed_model": "my-embed",
                }
            )
        )
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        # Reload the module so cached config-path defaults reset.
        from jarvis.config import load_settings

        settings = load_settings()

        assert settings.llm_provider == "ollama"
        assert settings.llm_base_url == "http://1.2.3.4:11434"
        assert settings.llm_chat_model == "my-model"
        assert settings.embedding_model == "my-embed"
        # Old fields stay populated for compatibility.
        assert settings.ollama_base_url == "http://1.2.3.4:11434"
        # Migration is persisted to disk.
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk["_config_version"] == 7
        assert on_disk["llm_provider"] == "ollama"
        assert on_disk["llm_base_url"] == "http://1.2.3.4:11434"

    def test_v1_to_v2_preserves_explicit_llm_fields(self, tmp_path, monkeypatch):
        """A user who has already opted into a non-Ollama provider
        before the migration runs (e.g. by hand-editing config) must
        not have their choice reverted by the v1→v2 promotion."""
        import json

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "_config_version": 1,
                    "llm_provider": "openai_compatible",
                    "llm_base_url": "http://lmstudio:1234/v1",
                    "llm_chat_model": "lmstudio-community/gemma",
                    "ollama_base_url": "http://127.0.0.1:11434",
                    "ollama_chat_model": "gemma4:e2b",
                }
            )
        )
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        from jarvis.config import load_settings

        settings = load_settings()

        assert settings.llm_provider == "openai_compatible"
        assert settings.llm_base_url == "http://lmstudio:1234/v1"
        assert settings.llm_chat_model == "lmstudio-community/gemma"

    def test_v1_to_v2_fresh_install_with_no_ollama_fields(self, tmp_path, monkeypatch):
        """A v1 config that never carried ``ollama_*`` keys should
        upgrade to v2 cleanly and fall through to defaults — no crash
        on missing keys."""
        import json

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"_config_version": 1}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        from jarvis.config import load_settings

        settings = load_settings()

        assert settings.llm_provider == "ollama"
        # Defaults flow through when nothing was explicitly set.
        assert settings.llm_base_url == settings.ollama_base_url
        # Migration runs without touching keys that have no source.
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk["_config_version"] == 7
        assert on_disk["llm_provider"] == "ollama"
