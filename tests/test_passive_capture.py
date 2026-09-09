"""Behavioural regression guards for the passive transcript record."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jarvis.listening.passive_capture import (
    capture_evicted_segment,
    clear_passive_buffer,
    register_passive_buffer,
    run_retention_sweep,
    set_passive_capture_enabled,
)
from jarvis.listening.transcript_buffer import TranscriptBuffer
from jarvis.memory.db import Database
from jarvis.runtime import get_runtime_state


@pytest.fixture(autouse=True)
def _reset_passive_switch():
    set_passive_capture_enabled(False)
    register_passive_buffer(None)
    get_runtime_state().reset()
    yield
    set_passive_capture_enabled(False)
    register_passive_buffer(None)


@pytest.fixture
def passive_cfg():
    return SimpleNamespace(
        passive_capture_min_words=3,
        passive_capture_retention_days=30,
    )


def _wire(db, cfg, *, duration=120.0):
    buffer = TranscriptBuffer(max_duration_sec=duration)
    buffer.set_eviction_sink(lambda segment: capture_evicted_segment(db, cfg, segment))
    register_passive_buffer(buffer)
    return buffer


def test_nothing_is_written_while_the_switch_is_off(db, passive_cfg):
    buffer = _wire(db, passive_cfg)
    now = datetime.now(timezone.utc).timestamp()
    buffer.add("three useful words", now - 2, now - 1, language="en")
    buffer.flush()

    assert db.list_passive_transcripts() == []


def test_evicted_segments_reach_the_record(db, passive_cfg):
    set_passive_capture_enabled(True)
    buffer = _wire(db, passive_cfg, duration=1.0)
    now = datetime.now(timezone.utc).timestamp()
    buffer.add("first useful ambient line", now - 5, now - 4, language="de")
    buffer.add("second useful ambient line", now - 4, now - 3, language="en")

    rows = db.list_passive_transcripts()
    assert [row["text"] for row in reversed(rows)] == [
        "first useful ambient line",
        "second useful ambient line",
    ]
    assert rows[0]["language"] == "en"


def test_echo_segments_are_never_written(db, passive_cfg):
    set_passive_capture_enabled(True)
    buffer = _wire(db, passive_cfg)
    now = datetime.now(timezone.utc).timestamp()
    buffer.add("assistant speech echoed back", now - 2, now - 1)
    assert buffer.mark_last_segment_echo()
    buffer.flush()

    assert db.list_passive_transcripts() == []


def test_short_utterances_are_dropped(db, passive_cfg):
    set_passive_capture_enabled(True)
    buffer = _wire(db, passive_cfg)
    now = datetime.now(timezone.utc).timestamp()
    buffer.add("one sec", now - 2, now - 1)
    buffer.flush()

    assert db.list_passive_transcripts() == []


def test_switching_off_drops_the_live_buffer(db, passive_cfg):
    set_passive_capture_enabled(True)
    buffer = _wire(db, passive_cfg)
    now = datetime.now(timezone.utc).timestamp()
    buffer.add("this must be forgotten", now - 2, now - 1)

    set_passive_capture_enabled(False)
    buffer.flush()

    assert len(buffer) == 0
    assert db.list_passive_transcripts() == []


def test_clearing_the_record_clears_the_buffer(db, passive_cfg):
    set_passive_capture_enabled(True)
    buffer = _wire(db, passive_cfg)
    now = datetime.now(timezone.utc).timestamp()
    buffer.add("pending live transcript words", now - 2, now - 1)

    db.delete_all_passive_transcripts()
    clear_passive_buffer()
    buffer.flush()

    assert len(buffer) == 0
    assert db.list_passive_transcripts() == []


def test_retention_deletes_old_lines(db, passive_cfg):
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    recent = now - timedelta(days=30)
    for stamp, text in ((old, "old retained speech"), (recent, "boundary retained speech")):
        db.insert_passive_transcript(
            ts_utc=stamp.isoformat(),
            date_utc=stamp.date().isoformat(),
            duration_sec=1.0,
            text=text,
            language="en",
            addressed=False,
            source_app="jarvis",
        )

    assert run_retention_sweep(db, passive_cfg, now=now) == 1
    assert [row["text"] for row in db.list_passive_transcripts()] == [
        "boundary retained speech"
    ]

    passive_cfg.passive_capture_retention_days = 0
    assert run_retention_sweep(db, passive_cfg, now=now + timedelta(days=365)) == 0
    assert len(db.list_passive_transcripts()) == 1


def test_a_failed_write_does_not_break_the_utterance(passive_cfg):
    class LockedDatabase:
        def insert_passive_transcript(self, **_fields):
            raise RuntimeError("database is locked")

    set_passive_capture_enabled(True)
    buffer = _wire(LockedDatabase(), passive_cfg)
    now = datetime.now(timezone.utc).timestamp()

    buffer.add("utterance continues after failure", now - 2, now - 1)
    buffer.flush()

    assert get_runtime_state().snapshot()["errors"] == 1

