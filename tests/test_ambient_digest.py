"""Behavioural regression guards for ambient digest processing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.memory import ambient
from jarvis.memory.db import Database


@pytest.fixture
def ambient_cfg():
    return SimpleNamespace(
        llm_chat_model="gemma4:e2b",
        llm_chat_timeout_sec=30.0,
        llm_thinking_enabled=False,
        passive_digest_max_lines=120,
        embedding_model="",
    )


def _insert(db: Database, text: str, *, addressed: bool = False) -> int:
    return db.insert_passive_transcript(
        ts_utc="2026-08-11T09:00:00+00:00",
        date_utc="2026-08-11",
        duration_sec=1.0,
        text=text,
        language="en",
        addressed=addressed,
        source_app="jarvis",
    )


def test_addressed_lines_are_not_digested(db, ambient_cfg, monkeypatch):
    row_id = _insert(db, "jarvis remember this request", addressed=True)
    monkeypatch.setattr(ambient, "_direct_llm", lambda *_a, **_kw: "unexpected")

    assert ambient.process_ambient_digest_once(db, ambient_cfg) is False
    row = db.list_passive_transcripts()[0]
    assert row["id"] == row_id
    assert row["digested"] == 0


def test_lines_stay_undigested_when_the_model_fails(db, ambient_cfg, monkeypatch):
    _insert(db, "someone mentioned a future appointment")
    monkeypatch.setattr(ambient, "_direct_llm", lambda *_a, **_kw: None)

    assert ambient.process_ambient_digest_once(db, ambient_cfg) is False
    assert db.list_passive_transcripts()[0]["digested"] == 0


def test_empty_digest_marks_lines_without_writing_a_diary_row(
    db, ambient_cfg, monkeypatch
):
    _insert(db, "ordinary room small talk")
    monkeypatch.setattr(ambient, "_direct_llm", lambda *_a, **_kw: "")

    assert ambient.process_ambient_digest_once(db, ambient_cfg) is True
    assert db.list_passive_transcripts()[0]["digested"] == 1
    assert db.get_conversation_summary("2026-08-11") is None


def test_ambient_text_is_fenced_and_redacted(ambient_cfg, monkeypatch):
    captured = {}

    def fake_direct(_cfg, system_prompt, user_prompt, **_kwargs):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return ""

    monkeypatch.setattr(ambient, "_direct_llm", fake_direct)
    digest = ambient.generate_ambient_digest(
        [
            {
                "ts_utc": "2026-08-11T09:00:00+00:00",
                "text": "token=super-secret-value and keep this appointment",
            }
        ],
        ambient_cfg,
    )

    assert digest == ""
    assert "<<<BEGIN UNTRUSTED WEB EXTRACT>>>" in captured["user"]
    assert "<<<END UNTRUSTED WEB EXTRACT>>>" in captured["user"]
    assert "super-secret-value" not in captured["user"]
    assert "[REDACTED]" in captured["user"]

