"""Both languages say the same things, with the same blanks to fill in.

A string added to one table and forgotten in the other does not fail: `t()`
falls back to English, so the interface half-translates itself and nobody
notices until a German speaker reads it. A placeholder that differs between
the tables is worse, because the value is dropped and the sentence renders
with a literal `{n}` in it.

Neither is visible from the running page, so it is checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


I18N = Path(__file__).resolve().parents[2] / "src/jarvis/webui/static/js/i18n.js"

ENTRY = re.compile(r'^\s*"([^"]+)":\s*"((?:[^"\\]|\\.)*)"\s*,?\s*$')
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _table(source: str, code: str) -> dict[str, str]:
    """The string table for one language, read out of the module source."""
    start = source.index(f"\n  {code}: {{")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                body = source[start:index]
                break
    else:  # pragma: no cover - only reachable if the file stops mid-table
        pytest.fail(f"the {code} table is never closed")

    entries = {}
    for line in body.splitlines():
        match = ENTRY.match(line)
        if match:
            entries[match.group(1)] = match.group(2)
    return entries


@pytest.fixture(scope="module")
def tables() -> dict[str, dict[str, str]]:
    source = I18N.read_text(encoding="utf-8")
    found = {code: _table(source, code) for code in ("en", "de")}
    # A parser that quietly reads nothing would pass every test below.
    assert len(found["en"]) > 100, "the English table did not parse"
    assert len(found["de"]) > 100, "the German table did not parse"
    return found


class TestBothLanguagesAreComplete:
    def test_german_says_everything_english_does(self, tables):
        missing = sorted(set(tables["en"]) - set(tables["de"]))

        assert not missing, f"untranslated, so these silently fall back to English: {missing}"

    def test_english_says_everything_german_does(self, tables):
        missing = sorted(set(tables["de"]) - set(tables["en"]))

        assert not missing, f"German-only keys, with no English to fall back to: {missing}"

    def test_no_string_is_left_empty(self, tables):
        blank = sorted(
            f"{code}:{key}"
            for code, table in tables.items()
            for key, text in table.items()
            if not text.strip()
        )

        assert not blank


class TestPlaceholdersMatch:
    def test_every_shared_key_takes_the_same_values(self, tables):
        mismatched = {}
        for key in set(tables["en"]) & set(tables["de"]):
            english = set(PLACEHOLDER.findall(tables["en"][key]))
            german = set(PLACEHOLDER.findall(tables["de"][key]))
            if english != german:
                mismatched[key] = (sorted(english), sorted(german))

        assert not mismatched, f"a caller cannot fill both: {mismatched}"
