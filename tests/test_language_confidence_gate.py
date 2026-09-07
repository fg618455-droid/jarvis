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
import time
from unittest.mock import MagicMock, patch

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


def _listener_gating(whisper_language, threshold, *, detection=("en", 0.41)):
    """A listener wired far enough to transcribe, with a scripted mic input.

    Whisper is scripted to hand back the hallucination shape: a transcript,
    a healthy confidence, and — because the language is pinned — a language
    probability of exactly 1.00. `detection` is what a separate
    identification pass over the same audio would report; ``None`` scripts a
    model that offers no way to run one.
    """
    import numpy as np

    model = MagicMock()
    segment = MagicMock()
    segment.text = "Hallu, Hallu, Jarvis, hvad mastu i gradir?"
    segment.avg_logprob = -0.2
    segment.no_speech_prob = 0.0
    info = MagicMock()
    info.language = whisper_language or "is"
    info.language_probability = 1.0
    model.transcribe.return_value = (iter([segment]), info)

    if detection is None:
        del model.feature_extractor
    else:
        extractor = MagicMock()
        extractor.return_value = np.zeros((128, 4000), dtype=np.float32)
        extractor.nb_max_frames = 3000
        model.feature_extractor = extractor
        model.model.detect_language.return_value = [
            [(f"<|{detection[0]}|>", detection[1])]
        ]

    with patch("jarvis.listening.listener.FASTER_WHISPER_AVAILABLE", True), \
            patch("jarvis.listening.listener.MLX_WHISPER_AVAILABLE", False), \
            patch("jarvis.listening.listener.WhisperModel"):
        from jarvis.listening.listener import VoiceListener

        cfg = MagicMock()
        cfg.sample_rate = 16000
        cfg.vad_enabled = False
        cfg.echo_tolerance = 0.3
        cfg.echo_energy_threshold = 2.0
        cfg.hot_window_seconds = 3.0
        cfg.voice_collect_seconds = 2.0
        cfg.voice_max_collect_seconds = 60.0
        cfg.tune_enabled = False
        cfg.voice_debug = False
        cfg.whisper_min_confidence = 0.3
        cfg.whisper_min_audio_duration = 0.15
        cfg.whisper_no_speech_threshold = 0.5
        cfg.whisper_min_language_probability = threshold
        cfg.whisper_language = whisper_language

        listener = VoiceListener(MagicMock(), cfg, MagicMock(), MagicMock())
        listener.model = model
        listener._whisper_backend = "faster-whisper"
        listener._whisper_device = "cuda"
        listener._samplerate = 16000
        listener._process_transcript = MagicMock()
        listener._utterance_frames = [np.zeros(16000, dtype=np.float32)]
        listener.echo_detector._utterance_start_time = time.time() - 1.0
        listener.is_speech_active = True

        return listener, model


def _dispatched(listener):
    """The text the listener passed on, or None if it discarded the utterance."""
    if not listener._process_transcript.called:
        return None
    return listener._process_transcript.call_args[0][0]


class TestTheGateHoldsUnderAPinnedLanguage:
    """Pinning a language must not silently disarm the gate.

    Naming a language skips Whisper's identification pass, so it reports a
    probability of 1.00 by definition and the gate can never fire. That is
    exactly the configuration a user reaches for after hearing German come
    back as Icelandic, and it is the configuration in which the only defence
    against a confident hallucination stops existing.
    """

    def test_a_hallucination_is_discarded_even_though_whisper_reported_certainty(self):
        listener, _model = _listener_gating("de", 0.85)

        listener._finalize_utterance()

        assert _dispatched(listener) is None

    def test_real_speech_in_the_pinned_language_still_gets_through(self):
        listener, _model = _listener_gating("de", 0.85, detection=("de", 0.99))

        listener._finalize_utterance()

        assert _dispatched(listener) is not None

    def test_the_pass_is_not_paid_when_the_user_did_not_arm_the_gate(self):
        """The identification pass costs real time on every utterance, so a
        user on the default threshold must not be charged for a gate they
        never switched on."""
        listener, model = _listener_gating("de", 0.0)

        listener._finalize_utterance()

        assert model.model.detect_language.called is False
        assert _dispatched(listener) is not None

    def test_an_unpinned_language_keeps_using_whispers_own_number(self):
        """Without a pin Whisper identifies the language anyway and reports a
        real probability, so a second pass would be paying twice."""
        listener, model = _listener_gating("", 0.85)

        listener._finalize_utterance()

        assert model.model.detect_language.called is False

    def test_a_model_that_cannot_be_asked_lets_the_utterance_through(self):
        """Fail open. The pass reaches past faster-whisper's public API, so a
        build that arranges its internals differently must cost the gate, not
        the user's sentence."""
        listener, _model = _listener_gating("de", 0.85, detection=None)

        listener._finalize_utterance()

        assert _dispatched(listener) is not None

    def test_the_probability_that_is_recorded_is_the_one_that_was_measured(self):
        """The journal is how this failure was found and how a repeat gets
        found again. A pinned turn filed as 1.00 says only that a language
        was named, which every pinned turn does; the number worth keeping is
        the one the identification pass actually returned."""
        from jarvis.runtime import get_recorder

        listener, _model = _listener_gating("de", 0.85, detection=("de", 0.93))

        # A turn is only filed once it has an answer, so the reading is taken
        # off the trace the journal entry will later be written from.
        recorder = get_recorder()
        opened = []
        began = recorder.begin

        def remember(**kwargs):
            opened.append(began(**kwargs))
            return opened[-1]

        with patch.object(recorder, "begin", side_effect=remember):
            listener._finalize_utterance()

        assert opened, "the utterance never opened a turn"
        assert opened[-1].language_probability == pytest.approx(0.93)
        assert opened[-1].language == "de"

    def test_a_discard_is_counted_under_the_reason_it_happened_for(self):
        """A silent discard is the usual cause of "it ignored me", so the
        count has to name this gate rather than land in a general bin."""
        from jarvis.runtime import get_runtime_state

        before = get_runtime_state().snapshot()["discarded"].get(
            "language_probability", 0
        )
        listener, _model = _listener_gating("de", 0.85)

        listener._finalize_utterance()

        after = get_runtime_state().snapshot()["discarded"].get(
            "language_probability", 0
        )
        assert after == before + 1

    def test_the_language_alone_never_decides(self):
        """The gate weighs the probability and never compares languages, so
        it holds for every language and for code-switching inside one. A
        confident reading that disagrees with the pin is still speech."""
        listener, _model = _listener_gating("de", 0.85, detection=("en", 0.99))

        listener._finalize_utterance()

        assert _dispatched(listener) is not None
