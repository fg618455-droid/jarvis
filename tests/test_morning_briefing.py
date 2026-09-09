"""Behavioural tests for the school morning briefing schedule."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

from jarvis.config import get_default_config
from jarvis.memory.ambient import AmbientDigestWorker
from jarvis.memory.db import Database
from jarvis.memory.morning_briefing import MorningBriefingScheduler


def _cfg(*, enabled: bool = True):
    return SimpleNamespace(
        morning_briefing_enabled=enabled,
        morning_briefing_time="07:00",
    )


def _scheduler(tmp_path, *, enabled=True, available=lambda: True):
    db = Database(str(tmp_path / "jarvis.db"))
    tts = MagicMock(enabled=True)
    read_school = MagicMock(return_value={"nodes": [{"data": "School fact"}]})
    generate = MagicMock(return_value="A generated briefing.")
    scheduler = MorningBriefingScheduler(
        db,
        _cfg(enabled=enabled),
        tts,
        is_available=available,
        read_school=read_school,
        generate=generate,
    )
    return scheduler, db, tts, read_school, generate


def _local(day: int, hour: int = 8):
    return datetime(2026, 9, day, hour, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def test_briefing_fires_once_and_only_once_per_local_day(tmp_path):
    scheduler, db, tts, _read, generate = _scheduler(tmp_path)
    try:
        assert scheduler.tick(now=_local(1)) is True
        assert scheduler.tick(now=_local(1, 10)) is False
        assert db.get_app_state("morning_briefing.last_delivered_local_date") == "2026-09-01"
        assert tts.speak.call_args_list == [call(generate.return_value)]
    finally:
        db.close()


def test_multi_day_gap_produces_one_briefing_for_today_not_catch_up_replays(tmp_path):
    scheduler, db, tts, _read, _generate = _scheduler(tmp_path)
    try:
        db.set_app_state("morning_briefing.last_delivered_local_date", "2026-08-28")
        assert scheduler.tick(now=_local(1)) is True
        assert scheduler.tick(now=_local(1, 11)) is False
        assert tts.speak.call_count == 1
        assert db.get_app_state("morning_briefing.last_delivered_local_date") == "2026-09-01"
    finally:
        db.close()


def test_once_per_day_gate_survives_a_database_reopen(tmp_path):
    scheduler, db, _tts, _read, _generate = _scheduler(tmp_path)
    assert scheduler.tick(now=_local(1)) is True
    db.close()

    restarted, reopened_db, restarted_tts, _read, _generate = _scheduler(tmp_path)
    try:
        assert restarted.tick(now=_local(1, 10)) is False
        restarted_tts.speak.assert_not_called()
    finally:
        reopened_db.close()


def test_briefing_defers_while_the_user_is_speaking(tmp_path):
    availability = MagicMock(side_effect=[False, True, True])
    scheduler, db, tts, _read, _generate = _scheduler(
        tmp_path,
        available=availability,
    )
    try:
        assert scheduler.tick(now=_local(1)) is False
        assert db.get_app_state("morning_briefing.last_delivered_local_date") is None
        tts.speak.assert_not_called()

        assert scheduler.tick(now=_local(1, 9)) is True
        tts.speak.assert_called_once()
    finally:
        db.close()


def test_disabled_by_default_does_no_scheduled_work(tmp_path):
    availability = MagicMock()
    scheduler, db, tts, read_school, generate = _scheduler(
        tmp_path,
        enabled=False,
        available=availability,
    )
    try:
        assert scheduler.tick(now=_local(1)) is False
        availability.assert_not_called()
        read_school.assert_not_called()
        generate.assert_not_called()
        tts.speak.assert_not_called()
        assert db.get_app_state("morning_briefing.last_delivered_local_date") is None
    finally:
        db.close()


def test_an_empty_school_branch_does_not_generate_or_speak(tmp_path):
    scheduler, db, tts, read_school, generate = _scheduler(tmp_path)
    read_school.return_value = {"branch": "school", "nodes": []}
    try:
        assert scheduler.tick(now=_local(1)) is False
        generate.assert_not_called()
        tts.speak.assert_not_called()
        assert db.get_app_state("morning_briefing.last_delivered_local_date") is None
    finally:
        db.close()


def test_stopped_scheduler_cannot_queue_speech(tmp_path):
    scheduler, db, tts, read_school, generate = _scheduler(tmp_path)
    try:
        scheduler.stop()

        assert scheduler.tick(now=_local(1)) is False
        read_school.assert_not_called()
        generate.assert_not_called()
        tts.speak.assert_not_called()
    finally:
        db.close()


def test_fresh_configuration_keeps_spoken_briefing_off():
    defaults = get_default_config()

    assert defaults["morning_briefing_enabled"] is False
    assert defaults["morning_briefing_time"] == "07:00"


def test_existing_ambient_worker_hosts_the_morning_gate():
    briefing = MagicMock()
    worker = AmbientDigestWorker(
        db=MagicMock(),
        cfg=SimpleNamespace(passive_digest_interval_min=15.0),
        morning_briefing=briefing,
    )

    worker.run_periodic_once(ambient_due=False)

    briefing.tick.assert_called_once_with()
