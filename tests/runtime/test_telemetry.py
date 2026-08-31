"""Behaviour tests for per-turn stage timings.

The recorder sits in the reply path, which is the one place in this project
where added latency is the bug. So the tests check three things: that the
measurements describe the turn truthfully, that history stays bounded, and
that the instrumentation itself costs nothing worth measuring.
"""

import json
import threading
import time

import pytest

from jarvis.runtime.state import Phase, get_runtime_state
from jarvis.runtime.telemetry import (
    HISTORY_SIZE,
    JOURNAL_MAX_BYTES,
    TurnRecorder,
    get_recorder,
    record_tool,
    stage,
)


@pytest.fixture
def recorder():
    rec = TurnRecorder()
    yield rec
    rec.abandon()
    rec.clear()


@pytest.fixture(autouse=True)
def _clean_global_state():
    get_recorder().abandon()
    get_recorder().clear()
    get_runtime_state().reset()
    yield
    get_recorder().abandon()
    get_recorder().clear()
    get_runtime_state().reset()


class TestMeasuringATurn:
    def test_stages_are_kept_in_the_order_they_happened(self, recorder):
        trace = recorder.begin()
        for name in ("stt", "recall", "llm", "tts"):
            with trace.stage(name):
                pass

        record = recorder.finish()

        assert [s["name"] for s in record["stages"]] == ["stt", "recall", "llm", "tts"]

    def test_each_stage_starts_after_the_one_before_it(self, recorder):
        trace = recorder.begin()
        for name in ("stt", "llm"):
            with trace.stage(name):
                time.sleep(0.01)

        record = recorder.finish()

        first, second = record["stages"]
        assert second["start_ms"] >= first["start_ms"] + first["duration_ms"] - 0.5

    def test_the_total_covers_every_stage(self, recorder):
        trace = recorder.begin()
        for name in ("stt", "llm"):
            with trace.stage(name):
                time.sleep(0.01)

        record = recorder.finish()

        assert record["total_ms"] >= sum(s["duration_ms"] for s in record["stages"])

    def test_a_stage_that_raises_is_still_measured(self, recorder):
        trace = recorder.begin()

        with pytest.raises(RuntimeError):
            with trace.stage("llm"):
                raise RuntimeError("model went away")

        record = recorder.finish(error="model went away")
        assert [s["name"] for s in record["stages"]] == ["llm"]
        assert record["error"] == "model went away"

    def test_a_duration_measured_elsewhere_can_be_handed_in(self, recorder):
        trace = recorder.begin()
        trace.mark("tts_first_audio", duration_ms=42.0)

        record = recorder.finish()

        assert record["stages"][0]["duration_ms"] == 42.0

    def test_tool_calls_are_attached_to_the_turn(self, recorder):
        trace = recorder.begin()
        trace.record_tool("webSearch", duration_ms=120.0, ok=True, confirmed=None)
        trace.record_tool("localFiles", duration_ms=5.0, ok=False, error="denied", confirmed=False)

        record = recorder.finish()

        assert [t["name"] for t in record["tools"]] == ["webSearch", "localFiles"]
        assert record["tools"][1]["ok"] is False
        assert record["tools"][1]["confirmed"] is False

    def test_the_transcript_and_reply_travel_with_the_turn(self, recorder):
        trace = recorder.begin(source="text")
        trace.transcript = "Wie spät ist es?"
        trace.language = "de"

        record = recorder.finish(reply="Es ist kurz nach drei.")

        assert record["source"] == "text"
        assert record["transcript"] == "Wie spät ist es?"
        assert record["language"] == "de"
        assert record["reply"] == "Es ist kurz nach drei."


class TestTurnsInParallel:
    def test_a_typed_turn_does_not_steal_a_spoken_turn_s_stages(self, recorder):
        """A turn belongs to its thread, so the two never mix."""
        spoken = recorder.begin(source="voice")
        started = threading.Event()

        def typed_turn():
            typed = recorder.begin(source="text")
            with typed.stage("llm"):
                pass
            started.set()
            recorder.finish(reply="typed")

        thread = threading.Thread(target=typed_turn)
        thread.start()
        started.wait(timeout=2.0)
        thread.join(timeout=2.0)

        with spoken.stage("stt"):
            pass
        record = recorder.finish(reply="spoken")

        assert [s["name"] for s in record["stages"]] == ["stt"]
        assert record["reply"] == "spoken"

    def test_the_current_turn_is_empty_outside_one(self, recorder):
        assert recorder.current() is None


class TestOutsideATurn:
    def test_measuring_a_stage_without_a_turn_does_nothing(self):
        with stage("llm"):
            pass  # must not raise

    def test_a_tool_call_without_a_turn_is_still_counted(self):
        record_tool("webSearch", duration_ms=10.0, ok=True)

        assert get_runtime_state().snapshot()["tool_calls"] == 1


class TestHistory:
    def test_history_holds_the_most_recent_turns(self, recorder):
        for index in range(HISTORY_SIZE + 10):
            trace = recorder.begin()
            trace.transcript = str(index)
            recorder.finish()

        history = recorder.history()

        assert len(history) == HISTORY_SIZE
        assert history[-1]["transcript"] == str(HISTORY_SIZE + 9)
        assert history[0]["transcript"] == str(10)

    def test_a_limit_returns_the_newest_end(self, recorder):
        for index in range(5):
            trace = recorder.begin()
            trace.transcript = str(index)
            recorder.finish()

        assert [t["transcript"] for t in recorder.history(limit=2)] == ["3", "4"]

    def test_an_abandoned_turn_never_reaches_history(self, recorder):
        recorder.begin()
        recorder.abandon()

        assert recorder.history() == []
        assert recorder.finish() is None


class TestJournal:
    def test_a_finished_turn_is_appended_as_one_line(self, recorder, tmp_path):
        journal = tmp_path / "turns.jsonl"
        recorder.use_journal(journal)

        for index in range(3):
            trace = recorder.begin()
            trace.transcript = f"turn {index}"
            recorder.finish()

        lines = journal.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[-1])["transcript"] == "turn 2"

    def test_an_oversized_journal_is_rotated_rather_than_grown(self, recorder, tmp_path):
        journal = tmp_path / "turns.jsonl"
        journal.write_text("x" * (JOURNAL_MAX_BYTES + 1), encoding="utf-8")
        recorder.use_journal(journal)

        recorder.begin()
        recorder.finish()

        assert journal.with_suffix(".jsonl.1").exists()
        assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == 1

    def test_an_unwritable_journal_does_not_break_the_turn(self, recorder, tmp_path):
        recorder.use_journal(tmp_path / "no" / "such" / "dir" / "\0bad.jsonl")

        recorder.begin()

        assert recorder.finish() is not None

    def test_no_journal_means_no_file(self, recorder, tmp_path):
        recorder.use_journal(None)

        recorder.begin()
        recorder.finish()

        assert list(tmp_path.iterdir()) == []

    def test_reader_combines_rotated_and_current_journals_in_order(self, tmp_path):
        from jarvis.runtime.telemetry import read_turn_journal

        journal = tmp_path / "turns.jsonl"
        rotated = journal.with_suffix(".jsonl.1")
        rotated.write_text(
            '\n'.join(json.dumps({"turn_id": str(index), "transcript": str(index)})
                      for index in range(3)) + "\n",
            encoding="utf-8",
        )
        journal.write_text(
            '{not-json}\n' + json.dumps({"turn_id": "3", "transcript": "3"}) + "\n",
            encoding="utf-8",
        )

        assert [turn["turn_id"] for turn in read_turn_journal(journal)] == [
            "0", "1", "2", "3",
        ]
        assert [turn["turn_id"] for turn in read_turn_journal(journal, limit=2)] == [
            "2", "3",
        ]


class TestCost:
    def test_measuring_a_turn_costs_well_under_a_millisecond(self, recorder):
        """Latency is the problem being worked on; the ruler must not weigh."""
        rounds = 1000

        begun = time.perf_counter()
        for _ in range(rounds):
            trace = recorder.begin()
            for name in ("endpoint", "stt", "recall", "llm_ttft", "tts_first_audio"):
                with trace.stage(name):
                    pass
            trace.record_tool("webSearch", duration_ms=1.0, ok=True)
            recorder.finish(reply="ok")
        per_turn_ms = (time.perf_counter() - begun) * 1000.0 / rounds

        assert per_turn_ms < 1.0, f"{per_turn_ms:.3f} ms per turn"


class TestStateIsKeptInStep:
    def test_a_finished_turn_counts_towards_its_source(self):
        recorder = get_recorder()
        recorder.begin(source="voice")
        recorder.finish()
        recorder.begin(source="text")
        recorder.finish()

        turns = get_runtime_state().snapshot()["turns"]
        assert turns == {"voice": 1, "text": 1, "total": 2}

    def test_the_last_turn_is_readable_from_the_state(self):
        recorder = get_recorder()
        trace = recorder.begin()
        trace.transcript = "hallo"
        recorder.finish(reply="hi")

        assert get_runtime_state().snapshot()["last_turn"]["transcript"] == "hallo"
