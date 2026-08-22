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

    def test_language_constraint_forbids_mid_reply_switching(self):
        prefix = build_reply_prompt_prefix(_Cfg(tts_engine="system"))

        assert "entire natural-language reply" in prefix
        assert "Do not switch languages mid-reply" in prefix

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


class TestTheCannedFallbackSpeaksTheVoicesLanguage:
    """A German voice reading an English apology is not a German assistant.

    The engine's malformed-output guard is the one reply the model never
    writes, so the language rule in the prompt cannot reach it. When the
    configured voice names a language, the message is rendered into it
    before it is spoken; nothing is guessed, and a failure leaves the
    English original standing rather than a half-translated sentence.
    """

    def test_a_named_voice_language_gets_the_message_rendered(self, tmp_path, mock_config):
        from jarvis.reply.fallbacks import in_the_voices_language, forget_renderings

        forget_renderings()
        mock_config.tts_engine = "piper"
        mock_config.tts_piper_model_path = _write_voice(
            tmp_path, "de_DE-thorsten-medium",
            {"code": "de_DE", "family": "de", "name_english": "German"},
        )
        asked = []

        def fake_render(cfg, language, message):
            asked.append(language)
            return "Ich habe die Anfrage nicht verstanden."

        assert in_the_voices_language(
            mock_config, "I had trouble understanding that request.",
            render=fake_render,
        ) == "Ich habe die Anfrage nicht verstanden."
        assert asked == ["German"]

    def test_a_rendering_is_reused_rather_than_asked_for_again(self, tmp_path, mock_config):
        from jarvis.reply.fallbacks import in_the_voices_language, forget_renderings

        forget_renderings()
        mock_config.tts_engine = "piper"
        mock_config.tts_piper_model_path = _write_voice(
            tmp_path, "de_DE-thorsten-medium",
            {"code": "de_DE", "family": "de", "name_english": "German"},
        )
        calls = []

        def fake_render(cfg, language, message):
            calls.append(message)
            return "Ich habe die Anfrage nicht verstanden."

        for _ in range(3):
            in_the_voices_language(mock_config, "I had trouble understanding that request.",
                                   render=fake_render)

        assert len(calls) == 1

    def test_no_named_language_leaves_the_message_alone(self, mock_config):
        """Text chat and an unreadable voice have nothing to render into."""
        from jarvis.reply.fallbacks import in_the_voices_language, forget_renderings

        forget_renderings()
        mock_config.tts_piper_model_path = None

        def fail(cfg, language, message):
            raise AssertionError("nothing should be asked without a named language")

        assert in_the_voices_language(mock_config, "I had trouble understanding that request.",
                                      render=fail) == \
            "I had trouble understanding that request."

    def test_a_failed_rendering_leaves_the_english_standing(self, tmp_path, mock_config):
        from jarvis.reply.fallbacks import in_the_voices_language, forget_renderings

        forget_renderings()
        mock_config.tts_engine = "piper"
        mock_config.tts_piper_model_path = _write_voice(
            tmp_path, "de_DE-thorsten-medium",
            {"code": "de_DE", "family": "de", "name_english": "German"},
        )

        def broken(cfg, language, message):
            raise RuntimeError("model unreachable")

        assert in_the_voices_language(mock_config, "I had trouble understanding that request.",
                                      render=broken) == \
            "I had trouble understanding that request."
