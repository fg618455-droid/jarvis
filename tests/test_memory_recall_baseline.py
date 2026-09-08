"""Tests for the memory recall baseline harness.

The harness measures Recall@3 of the retrieval layer against a fixed corpus
and a fixed question catalogue. Its numbers are the reference the deterministic
replacement is judged against, so the scoring itself has to be trustworthy: a
harness that quietly counts a miss as a hit would make any later regression
invisible.

The tests run on stub embedders, so they need no embedding backend.
"""

import json
from pathlib import Path

import pytest

from evals.baselines.memory_recall import (
    CORPUS,
    QUESTIONS,
    BASELINE_PATH,
    measure_recall,
)

pytestmark = pytest.mark.unit


def _perfect_embedder(corpus_by_key):
    """Embed so that every question lands exactly on its expected document."""
    keys = sorted(corpus_by_key)
    index = {key: i for i, key in enumerate(keys)}

    def embed(text: str):
        vec = [0.0] * len(keys)
        for key, doc in corpus_by_key.items():
            if doc.summary == text:
                vec[index[key]] = 1.0
                return vec
        for question in QUESTIONS:
            if " ".join(question.keywords) == text:
                vec[index[question.expected]] = 1.0
                return vec
        return vec

    return embed


def _useless_embedder(_corpus_by_key):
    """Every text gets the same vector, so the vector half ranks at random."""
    return lambda text: [1.0] * 8


class TestScoring:
    def test_a_better_embedder_scores_higher(self, tmp_path):
        """The score has to answer to embedding quality, or it measures nothing."""
        corpus_by_key = {doc.key: doc for doc in CORPUS}

        good = measure_recall(
            tmp_path / "perfect.db", _perfect_embedder(corpus_by_key), k=3
        )
        useless = measure_recall(tmp_path / "useless.db", _useless_embedder(None), k=3)

        assert good["recall_at_k"] > useless["recall_at_k"]

    def test_recall_is_bounded_and_counts_every_question(self, tmp_path):
        report = measure_recall(
            tmp_path / "useless.db", _useless_embedder(None), k=3
        )

        assert report["questions"] == len(QUESTIONS)
        assert report["hits"] + report["misses"] == len(QUESTIONS)
        assert 0.0 <= report["recall_at_k"] <= 1.0

    def test_recall_is_split_by_question_kind(self, tmp_path):
        corpus_by_key = {doc.key: doc for doc in CORPUS}

        report = measure_recall(
            tmp_path / "perfect.db", _perfect_embedder(corpus_by_key), k=3
        )

        assert set(report["recall_by_kind"]) == {q.kind for q in QUESTIONS}
        per_kind_hits = sum(
            round(value * sum(q.kind == kind for q in QUESTIONS))
            for kind, value in report["recall_by_kind"].items()
        )
        assert per_kind_hits == report["hits"], "the split must add up to the total"

    def test_every_miss_is_named(self, tmp_path):
        report = measure_recall(
            tmp_path / "useless.db", _useless_embedder(None), k=3
        )

        assert len(report["missed_queries"]) == report["misses"]
        for query in report["missed_queries"]:
            assert query in {q.query for q in QUESTIONS}

    def test_a_narrower_k_cannot_score_higher(self, tmp_path):
        embed = _useless_embedder(None)

        at_one = measure_recall(tmp_path / "k1.db", embed, k=1)
        at_five = measure_recall(tmp_path / "k5.db", embed, k=5)

        assert at_one["recall_at_k"] <= at_five["recall_at_k"]


class TestCatalogue:
    def test_every_question_points_at_a_document_in_the_corpus(self):
        keys = {doc.key for doc in CORPUS}
        for question in QUESTIONS:
            assert question.expected in keys, question.query

    def test_document_keys_and_dates_are_unique(self):
        keys = [doc.key for doc in CORPUS]
        dates = [doc.date for doc in CORPUS]
        assert len(keys) == len(set(keys))
        assert len(dates) == len(set(dates)), "one row per date, or upsert overwrites"

    def test_the_catalogue_spans_more_than_one_language(self):
        """Retrieval must not be tuned to English: the corpus is mixed."""
        languages = {doc.language for doc in CORPUS}
        assert len(languages) > 1


class TestRecordedBaseline:
    def test_the_baseline_file_is_present_and_matches_the_catalogue(self):
        assert BASELINE_PATH.exists(), (
            f"Missing {BASELINE_PATH}. Regenerate it with "
            "`python -m evals.baselines.memory_recall`."
        )
        recorded = json.loads(Path(BASELINE_PATH).read_text(encoding="utf-8"))

        assert recorded["questions"] == len(QUESTIONS)
        assert recorded["corpus_size"] == len(CORPUS)
        assert recorded["k"] == 3
        assert 0.0 <= recorded["recall_at_k"] <= 1.0
        assert recorded["embedding_model"], "the measured model must be named"
        assert recorded["retrieval"] == "hybrid", (
            "the baseline records the hybrid search, not its replacement"
        )
        assert set(recorded["recall_by_kind"]) == {q.kind for q in QUESTIONS}
        assert 0.0 <= recorded["fts_only"]["recall_at_k"] <= 1.0, (
            "the keyword-only branch is the reference a replacement is judged against"
        )
