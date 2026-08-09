"""Behaviour tests for the language-confidence gate on transcriptions.

Whisper invents short filler phrases ("Thank you.", "Okay.") from room noise.
Those hallucinations carry a high `avg_logprob` and a `no_speech_prob` of
zero, so neither existing filter catches them. What does separate them from
real speech is language identification: Whisper is sure which language real
speech is in and unsure about noise.

Measured on a German user's microphone: real speech identified as `de` at
1.00, five consecutive noise hallucinations identified as `en` at 0.46-0.76.
"""

import json

import pytest

from jarvis.config import load_settings
from jarvis.listening.listener import is_uncertain_language


class TestIsUncertainLanguage:
    @pytest.mark.parametrize("probability", [0.46, 0.52, 0.59, 0.75, 0.76])
    def test_measured_hallucination_probabilities_are_rejected(self, probability):
        assert is_uncertain_language(probability, 0.85) is True

    @pytest.mark.parametrize("probability", [0.85, 0.9, 0.99, 1.0])
    def test_confident_identification_passes(self, probability):
        assert is_uncertain_language(probability, 0.85) is False

    def test_threshold_of_zero_disables_the_gate(self):
        """Default is off, so no existing setup changes behaviour on upgrade."""
        assert is_uncertain_language(0.01, 0.0) is False

    @pytest.mark.parametrize("probability", [None, "high"])
    def test_missing_or_malformed_probability_passes(self, probability):
        """Fail open: a transcription we cannot judge is not thrown away."""
        assert is_uncertain_language(probability, 0.85) is False


class TestTheConfiguredThresholdSurvivesLoading:
    """A threshold the user writes down has to reach the code that reads it.

    The listener reads the threshold off the settings object, so a setting that
    parses into a default dictionary but never onto that object leaves the gate
    permanently open while every unit test on the gate itself stays green.
    """

    def _load_with(self, tmp_path, monkeypatch, values):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(values))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        return load_settings()

    def test_measured_hallucination_is_rejected_under_a_configured_threshold(
        self, tmp_path, monkeypatch
    ):
        cfg = self._load_with(tmp_path, monkeypatch, {"whisper_min_language_probability": 0.85})

        threshold = getattr(cfg, "whisper_min_language_probability", 0.0)

        assert is_uncertain_language(0.52, threshold) is True

    def test_default_configuration_leaves_the_gate_open(self, tmp_path, monkeypatch):
        cfg = self._load_with(tmp_path, monkeypatch, {})

        threshold = getattr(cfg, "whisper_min_language_probability", 0.0)

        assert is_uncertain_language(0.46, threshold) is False
