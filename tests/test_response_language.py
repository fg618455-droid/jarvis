"""Behaviour tests for the response-language constraint.

The reply engine tells the model which language to answer in so the spoken
output matches the configured voice. The constraint is derived from the
voice's own metadata rather than assumed, so any Piper voice works without
a code change.

A voice is not the only way in. Text chat runs the same engine with no TTS
at all, and a voice whose metadata cannot be read leaves nothing to derive
from. In those cases the reply still has to land in the language the user
used, so the prompt asks the model to mirror the user rather than falling
silent and letting it drift to English.
"""

import json

import pytest

from jarvis.output.tts import resolve_voice_language
from jarvis.reply.engine import build_reply_prompt_prefix


def _write_voice(tmp_path, name, language_block):
    model = tmp_path / f"{name}.onnx"
    model.write_bytes(b"not-a-real-model")
    config = tmp_path / f"{name}.onnx.json"
    payload = {"audio": {"sample_rate": 22050}}
    if language_block is not None:
        payload["language"] = language_block
    config.write_text(json.dumps(payload), encoding="utf-8")
    return str(model)


class TestResolveVoiceLanguage:
    def test_german_voice_reports_german(self, tmp_path):
        path = _write_voice(
            tmp_path,
            "de_DE-thorsten-medium",
            {"code": "de_DE", "family": "de", "name_english": "German"},
        )

        assert resolve_voice_language(path) == "German"

    def test_english_voice_reports_english(self, tmp_path):
        path = _write_voice(
            tmp_path,
            "en_GB-alba-medium",
            {"code": "en_GB", "family": "en", "name_english": "English"},
        )

        assert resolve_voice_language(path) == "English"

    @pytest.mark.parametrize("language_block", [None, {}, {"code": "xx_XX"}])
    def test_unknown_metadata_yields_no_constraint(self, tmp_path, language_block):
        """Without a usable name we say nothing rather than guess a language."""
        path = _write_voice(tmp_path, "mystery-voice", language_block)

        assert resolve_voice_language(path) is None

    def test_missing_config_file_yields_no_constraint(self, tmp_path):
        assert resolve_voice_language(str(tmp_path / "absent.onnx")) is None

    def test_no_path_yields_no_constraint(self):
        assert resolve_voice_language(None) is None

    def test_malformed_config_yields_no_constraint(self, tmp_path):
        model = tmp_path / "broken.onnx"
        model.write_bytes(b"x")
        (tmp_path / "broken.onnx.json").write_text("{not json", encoding="utf-8")

        assert resolve_voice_language(str(model)) is None


class _Cfg:
    """The handful of fields the reply prefix reads."""

    wake_word = "jarvis"
    llm_chat_model = "qwen2.5:7b"

    def __init__(self, **fields):
        self.tts_engine = "piper"
        self.tts_piper_model_path = None
        for key, value in fields.items():
            setattr(self, key, value)


def _mirrors_the_user(prefix: str) -> bool:
    """Whether the prefix asks the model to answer in the user's own language."""
    return "same language" in prefix.lower()


class TestReplyPromptLanguageConstraint:
    """What the prompt says about response language, for every TTS setting."""

    def test_named_voice_pins_the_reply_to_that_language(self, tmp_path):
        path = _write_voice(
            tmp_path,
            "de_DE-thorsten-medium",
            {"code": "de_DE", "family": "de", "name_english": "German"},
        )

        prefix = build_reply_prompt_prefix(_Cfg(tts_piper_model_path=path))

        assert "Always respond in German" in prefix

    def test_unreadable_voice_still_constrains_the_language(self, tmp_path):
        """A voice path that points at nothing must not silently drop the rule.

        This is the failure a stale ``tts_piper_model_path`` produces: the
        sidecar cannot be read, no language is named, and without a fallback
        the model answers a German user in English.
        """
        prefix = build_reply_prompt_prefix(
            _Cfg(tts_piper_model_path=str(tmp_path / "absent.onnx")),
        )

        assert _mirrors_the_user(prefix)

    def test_no_voice_configured_still_constrains_the_language(self):
        prefix = build_reply_prompt_prefix(_Cfg(tts_piper_model_path=None))

        assert _mirrors_the_user(prefix)

    def test_text_chat_without_tts_still_constrains_the_language(self):
        """Text chat runs the engine with no speech anywhere in the picture."""
        prefix = build_reply_prompt_prefix(_Cfg(tts_engine="system"))

        assert _mirrors_the_user(prefix)

    def test_english_only_engine_keeps_its_english_rule(self):
        """Chatterbox cannot speak anything else, so English stays pinned."""
        prefix = build_reply_prompt_prefix(_Cfg(tts_engine="chatterbox"))

        assert "Always respond in English" in prefix
        assert not _mirrors_the_user(prefix)

    def test_a_named_voice_does_not_also_ask_for_mirroring(self, tmp_path):
        """One rule at a time: a named language and 'mirror the user' conflict."""
        path = _write_voice(
            tmp_path,
            "de_DE-thorsten-medium",
            {"code": "de_DE", "family": "de", "name_english": "German"},
        )

        prefix = build_reply_prompt_prefix(_Cfg(tts_piper_model_path=path))

        assert not _mirrors_the_user(prefix)
