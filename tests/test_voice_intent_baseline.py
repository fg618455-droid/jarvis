"""Tests for the voice intent baseline harness.

The harness scores the existing intent-judge eval catalogue twice: once with
the LLM judge and once with the deterministic rules that already run when the
judge is unavailable. The gap between the two is what the rewrite has to close,
so the scoring has to be exact about what counts as a pass.

The rule engine needs no model, so these tests run offline.
"""

import json
from pathlib import Path

import pytest

from evals.baselines.voice_intent import (
    BASELINE_PATH,
    Verdict,
    judge_by_rules,
    load_cases,
    score,
)

pytestmark = pytest.mark.unit


def _case(name):
    for case in load_cases():
        if case.name == name:
            return case
    raise AssertionError(f"unknown case {name}")


class TestPassCriteria:
    def test_a_matching_verdict_passes(self):
        case = _case("wake_word_simple_question")

        assert score(case, Verdict(directed=True, query="what time is it", stop=False))

    def test_the_wrong_directedness_fails(self):
        case = _case("wake_word_simple_question")

        assert not score(case, Verdict(directed=False, query="", stop=False))

    def test_a_missing_required_substring_fails(self):
        case = _case("wake_word_simple_question")

        assert not score(case, Verdict(directed=True, query="what is it", stop=False))

    def test_a_forbidden_substring_fails(self):
        case = _case("wake_word_simple_question")

        assert not score(
            case, Verdict(directed=True, query="jarvis what time is it", stop=False)
        )

    def test_a_missing_verdict_fails(self):
        case = _case("wake_word_simple_question")

        assert not score(case, None)


class TestRules:
    def test_a_wake_word_makes_an_utterance_directed(self):
        verdict = judge_by_rules(_case("wake_word_simple_question"))

        assert verdict.directed
        assert "jarvis" not in verdict.query.lower()

    def test_without_a_wake_word_nothing_is_directed(self):
        """Outside the hot window, silence is the default. Rules cannot infer
        that an utterance was aimed at the assistant."""
        cases = [c for c in load_cases() if not c.in_hot_window and c.wake_timestamp is None]
        assert cases, "the catalogue must cover unaddressed speech"

        for case in cases:
            verdict = judge_by_rules(case)
            if not any(
                w in case.text.lower() for w in ("jarvis", "joris", "jervis", "javis")
            ):
                assert not verdict.directed, case.name

    def test_the_rules_never_raise(self):
        for case in load_cases():
            assert isinstance(judge_by_rules(case), Verdict), case.name


class TestCatalogue:
    def test_both_case_shapes_are_loaded(self):
        kinds = {case.kind for case in load_cases()}

        assert kinds == {"single", "multi"}

    def test_case_names_are_unique(self):
        names = [case.name for case in load_cases()]

        assert len(names) == len(set(names))


class TestRecordedBaseline:
    def test_the_baseline_file_is_present_and_matches_the_catalogue(self):
        assert BASELINE_PATH.exists(), (
            f"Missing {BASELINE_PATH}. Regenerate it with "
            "`python -m evals.baselines.voice_intent`."
        )
        recorded = json.loads(Path(BASELINE_PATH).read_text(encoding="utf-8"))

        assert recorded["cases"] == len(load_cases())
        for engine in ("rules", "intent_judge"):
            assert 0.0 <= recorded[engine]["accuracy"] <= 1.0
        assert recorded["intent_judge"]["model"], "the measured judge model must be named"

    def test_the_recorded_gap_matches_the_recorded_scores(self):
        recorded = json.loads(Path(BASELINE_PATH).read_text(encoding="utf-8"))

        gap = recorded["intent_judge"]["accuracy"] - recorded["rules"]["accuracy"]
        assert recorded["gap_percentage_points"] == pytest.approx(round(gap * 100, 2))
