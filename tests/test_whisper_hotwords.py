"""Behaviour tests for naming words Whisper has no reason to expect.

A general German model transcribes an unexpected proper noun as the nearest
ordinary word: "Vault" comes back as "Volt", "Beug" or "Woll". The sentence
still looks plausible, so nothing downstream can tell it went wrong, and the
tool router never sees the term that would have selected the right tool.

Which words those are depends entirely on the user's own vocabulary, so the
list belongs in config rather than in the code. The wake word is always
included because it opens every utterance.
"""

import json

from jarvis.config import load_settings


def _load_with(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


class TestConfiguredHotwords:
    def test_default_covers_the_vault_vocabulary(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {})

        assert "Vault" in cfg.whisper_hotwords
        assert "Obsidian" in cfg.whisper_hotwords

    def test_a_user_can_name_their_own_words(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "whisper_hotwords": ["SchulOS", "Marquartstein"],
        })

        assert cfg.whisper_hotwords == ["SchulOS", "Marquartstein"]

    def test_blank_entries_are_dropped(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "whisper_hotwords": ["Vault", "  ", "", "Obsidian"],
        })

        assert cfg.whisper_hotwords == ["Vault", "Obsidian"]

    def test_a_single_word_need_not_be_a_list(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {"whisper_hotwords": "Vault"})

        assert cfg.whisper_hotwords == ["Vault"]

    def test_an_empty_list_is_honoured(self, tmp_path, monkeypatch):
        """Switching the bias off entirely must stay possible."""
        cfg = _load_with(tmp_path, monkeypatch, {"whisper_hotwords": []})

        assert cfg.whisper_hotwords == []


class TestHotwordStringHandedToWhisper:
    def _listener(self, cfg):
        from jarvis.listening.listener import VoiceListener

        listener = VoiceListener.__new__(VoiceListener)
        listener.cfg = cfg
        return listener

    def test_wake_word_leads_and_configured_words_follow(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "wake_word": "jarvis",
            "whisper_hotwords": ["Vault", "Obsidian"],
        })

        assert self._listener(cfg)._transcription_hotwords() == "Jarvis Vault Obsidian"

    def test_the_wake_word_is_not_repeated(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "wake_word": "jarvis",
            "whisper_hotwords": ["Jarvis", "Vault"],
        })

        assert self._listener(cfg)._transcription_hotwords() == "Jarvis Vault"

    def test_without_configured_words_the_wake_word_remains(self, tmp_path, monkeypatch):
        cfg = _load_with(tmp_path, monkeypatch, {
            "wake_word": "computer",
            "whisper_hotwords": [],
        })

        assert self._listener(cfg)._transcription_hotwords() == "Computer"
