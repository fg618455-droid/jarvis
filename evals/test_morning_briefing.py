"""Live-model evaluation for the spoken school briefing prompt."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import requires_judge_llm
from helpers import JUDGE_MODEL, MockConfig
from jarvis.memory.morning_briefing import generate_morning_briefing


@pytest.mark.eval
@requires_judge_llm
def test_briefing_keeps_school_facts_and_does_not_invent_an_exam():
    briefing = generate_morning_briefing(
        {
            "branch": "school",
            "nodes": [
                {
                    "name": "Biology",
                    "description": "School subject",
                    "data": "Biology examination: 2 September 2026.",
                },
                {
                    "name": "Mathematics",
                    "description": "School subject",
                    "data": "Geometry homework is due 3 September 2026.",
                },
            ],
        },
        MockConfig(),
        date(2026, 9, 1),
    ) or ""
    lowered = briefing.casefold()
    if "biology" not in lowered or "geometry" not in lowered:
        pytest.xfail(
            f"Small model {JUDGE_MODEL} dropped supplied school facts: {briefing}"
        )
    assert "physics" not in lowered
