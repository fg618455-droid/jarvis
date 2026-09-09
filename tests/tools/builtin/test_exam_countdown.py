"""Behavioural tests for the school exam countdown tool."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from jarvis.memory.graph import GraphMemoryStore
from jarvis.memory.school_context import read_school_branch
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.exam_countdown import ExamCountdownTool


def _context(db_path):
    return ToolContext(
        db=SimpleNamespace(db_path=str(db_path)),
        cfg=SimpleNamespace(
            db_path=str(db_path),
            fast_model="test-fast",
            llm_tools_timeout_sec=5.0,
        ),
        system_prompt="",
        original_prompt="",
        redacted_text="",
        max_retries=0,
        user_print=lambda _message: None,
    )


def _school_fact(db_path, *, subject: str, fact: str) -> None:
    store = GraphMemoryStore(str(db_path))
    try:
        store.create_node(subject, "School subject", fact, parent_id="school")
    finally:
        store.close()


def _backend_returning(payload):
    backend = MagicMock()
    backend.direct.return_value = json.dumps(payload)
    return backend


def test_days_remaining_uses_the_users_local_day_boundary(tmp_path):
    db_path = tmp_path / "jarvis.db"
    _school_fact(
        db_path,
        subject="Biology",
        fact="The biology exam is on 2 September 2026.",
    )
    backend = _backend_returning(
        [
            {
                "subject": "Biology",
                "date": "2 September 2026",
                "date_iso": "2026-09-02",
            }
        ]
    )
    local_now = datetime(2026, 9, 1, 0, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    tool = ExamCountdownTool(now_provider=lambda _cfg: local_now)

    with patch(
        "jarvis.tools.builtin.exam_countdown.get_llm_backend",
        return_value=backend,
    ):
        result = tool.run({}, _context(db_path))

    assert result.success is True
    payload = json.loads(result.reply_text)
    assert payload["as_of_date"] == local_now.date().isoformat()
    assert payload["exams"] == [
        {
            "subject": "Biology",
            "date": "2 September 2026",
            "days_remaining": (
                datetime(2026, 9, 2).date() - local_now.date()
            ).days,
        }
    ]


def test_an_unparseable_date_surfaces_as_unknown_instead_of_accepting_a_guess(tmp_path):
    db_path = tmp_path / "jarvis.db"
    raw_date = "sometime after the break"
    _school_fact(db_path, subject="Chemistry", fact=f"Chemistry exam {raw_date}.")
    backend = _backend_returning(
        [
            {
                "subject": "Chemistry",
                "date": raw_date,
                # A model guess is deliberately unsupported by the source text.
                "date_iso": "2026-09-14",
            }
        ]
    )
    tool = ExamCountdownTool(
        now_provider=lambda _cfg: datetime(
            2026, 9, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")
        )
    )

    with patch(
        "jarvis.tools.builtin.exam_countdown.get_llm_backend",
        return_value=backend,
    ):
        result = tool.run({}, _context(db_path))

    assert json.loads(result.reply_text)["exams"] == [
        {
            "subject": "Chemistry",
            "date": raw_date,
            "days_remaining": None,
        }
    ]


def test_an_exam_date_invented_by_the_extractor_is_not_returned(tmp_path):
    db_path = tmp_path / "jarvis.db"
    _school_fact(db_path, subject="Biology", fact="Revise cell structure.")
    backend = _backend_returning(
        [
            {
                "subject": "Physics",
                "date": "14 September 2026",
                "date_iso": "2026-09-14",
            }
        ]
    )
    tool = ExamCountdownTool(
        now_provider=lambda _cfg: datetime(
            2026, 9, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")
        )
    )

    with patch(
        "jarvis.tools.builtin.exam_countdown.get_llm_backend",
        return_value=backend,
    ):
        result = tool.run({}, _context(db_path))

    assert json.loads(result.reply_text)["exams"] == []


def test_an_empty_school_branch_answers_cleanly_without_a_model_call(tmp_path):
    db_path = tmp_path / "jarvis.db"
    store = GraphMemoryStore(str(db_path))
    store.close()
    backend = MagicMock()
    tool = ExamCountdownTool(
        now_provider=lambda _cfg: datetime(
            2026, 9, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")
        )
    )

    with patch(
        "jarvis.tools.builtin.exam_countdown.get_llm_backend",
        return_value=backend,
    ):
        result = tool.run({}, _context(db_path))

    assert result.success is True
    assert json.loads(result.reply_text) == {
        "as_of_date": "2026-09-01",
        "exams": [],
    }
    backend.direct.assert_not_called()


def test_school_snapshot_honours_its_character_budget(tmp_path):
    db_path = tmp_path / "jarvis.db"
    _school_fact(db_path, subject="Biology", fact="x" * 500)

    snapshot = read_school_branch(str(db_path), max_chars=40)

    assert sum(
        len(value)
        for node in snapshot["nodes"]
        for value in node.values()
    ) <= 40
