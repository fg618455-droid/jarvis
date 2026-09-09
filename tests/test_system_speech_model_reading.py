"""
Tests for how the system view reports the speech model.

The Whisper setting holds a model name ("base", "large-v3-turbo"), not a
path. Checking it as a path always fails, so a working install reported its
own transcription as missing while it was busy transcribing.
"""

from pathlib import Path

from jarvis.webui.api.system import _speech_model_reading


class TestSpeechModelReading:
    def test_a_cached_model_is_reported_as_present(self, tmp_path):
        cache = tmp_path / "hub"
        (cache / "models--Systran--faster-whisper-base").mkdir(parents=True)

        reading = _speech_model_reading("base", cache_root=cache)

        assert reading["label"] == "speech model"
        assert reading["name"] == "base"
        assert reading["exists"] is True

    def test_an_uncached_model_is_reported_as_absent(self, tmp_path):
        cache = tmp_path / "hub"
        cache.mkdir(parents=True)

        reading = _speech_model_reading("large-v3-turbo", cache_root=cache)

        assert reading["exists"] is False

    def test_a_longer_name_does_not_match_a_shorter_cached_one(self, tmp_path):
        """'base' must not satisfy a request for 'base.en'."""
        cache = tmp_path / "hub"
        (cache / "models--Systran--faster-whisper-base").mkdir(parents=True)

        assert _speech_model_reading("base.en", cache_root=cache)["exists"] is False

    def test_a_missing_cache_directory_is_not_an_error(self, tmp_path):
        reading = _speech_model_reading("base", cache_root=tmp_path / "nope")

        assert reading["exists"] is False

    def test_a_path_shaped_setting_is_honoured_as_a_path(self, tmp_path):
        """Some setups point straight at a converted model directory."""
        model_dir = tmp_path / "my-whisper"
        model_dir.mkdir()

        reading = _speech_model_reading(str(model_dir), cache_root=tmp_path / "hub")

        assert reading["exists"] is True

    def test_an_empty_setting_reports_nothing_rather_than_guessing(self, tmp_path):
        assert _speech_model_reading("", cache_root=tmp_path)["exists"] is False
