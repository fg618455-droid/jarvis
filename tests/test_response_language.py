"""Behaviour tests for the response-language constraint.

The reply engine tells the model which language to answer in so the spoken
output matches the configured voice. The constraint is derived from the
voice's own metadata rather than assumed, so any Piper voice works without
a code change.
"""

import json

import pytest

from jarvis.output.tts import resolve_voice_language


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
