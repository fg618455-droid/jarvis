"""Every theme retunes the same instrument.

The visual language rule is that no view carries a literal: a colour, a size,
or a duration is a named token. A theme system makes that rule load-bearing
in a second way. A theme that forgets a token does not fail loudly; the token
falls back to whatever the previous theme left on the cascade, or to nothing
at all, and one card in the interface quietly reads in the wrong palette.

So the check is a mechanism rather than a list of values: whatever set of
colour tokens the base theme names, every other theme names the same set.
Adding a token to one theme and forgetting it in another is the failure this
catches, and it catches it for themes that do not exist yet.
"""

from __future__ import annotations

import re
from pathlib import Path


STATIC = Path(__file__).resolve().parents[2] / "src/jarvis/webui/static"
TOKENS = STATIC / "css/tokens.css"
THEME_JS = STATIC / "js/theme.js"
INDEX = STATIC / "index.html"

# `--name: value;` at the start of a line inside a block.
DECLARATION = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)
# `[data-theme="name"] {` opens a theme block.
THEME_BLOCK = re.compile(r'\[data-theme=["\']([a-z0-9-]+)["\']\]\s*\{')
ROOT_BLOCK = re.compile(r"^:root\s*\{", re.MULTILINE)


def _block_body(source: str, opening_brace_index: int) -> str:
    """The text between a block's braces, honouring nesting."""
    depth = 0
    for index in range(opening_brace_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace_index + 1 : index]
    raise AssertionError("a block in tokens.css is never closed")


def _source() -> str:
    return TOKENS.read_text(encoding="utf-8")


def _root_tokens(source: str) -> set[str]:
    match = ROOT_BLOCK.search(source)
    assert match, ":root did not parse"
    return set(DECLARATION.findall(_block_body(source, match.end() - 1)))


def _themes(source: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for match in THEME_BLOCK.finditer(source):
        body = _block_body(source, match.end() - 1)
        found[match.group(1)] = set(DECLARATION.findall(body))
    return found


class TestThemesAreInterchangeable:
    def test_more_than_one_theme_exists(self):
        themes = _themes(_source())

        assert len(themes) >= 2, f"a theme picker needs themes to pick: {sorted(themes)}"

    def test_the_established_look_is_still_one_of_them(self):
        """The interface people already use does not vanish behind a redesign."""
        assert "graphite" in _themes(_source())

    def test_every_theme_names_every_token_the_others_do(self):
        themes = _themes(_source())
        complete = set().union(*themes.values())

        missing = {
            name: sorted(complete - tokens)
            for name, tokens in themes.items()
            if complete - tokens
        }

        assert not missing, f"these themes fall back to another theme's colour: {missing}"

    def test_a_theme_carries_colour_rather_than_structure(self):
        """Type, spacing, and motion are the instrument, not the paint.

        A theme that redefined the type scale would make a heading change
        size when the palette changed, which is a different feature wearing
        a theme's clothes.
        """
        structural = {"--fs-", "--s0", "--s1", "--s2", "--s3", "--s4", "--s5",
                      "--s6", "--t-", "--r", "--r-sm", "--font", "--mono"}
        offenders = {}
        for name, tokens in _themes(_source()).items():
            bad = sorted(
                token for token in tokens
                if any(token.startswith(prefix) or token == prefix for prefix in structural)
            )
            if bad:
                offenders[name] = bad

        assert not offenders, f"a theme is redefining structure: {offenders}"

    def test_structure_is_defined_once_for_every_theme(self):
        root = _root_tokens(_source())

        assert "--fs-base" in root, "the type scale left :root"
        assert "--s4" in root, "the spacing scale left :root"
        assert "--t-fast" in root, "the motion scale left :root"


class TestAThemeIsKnownEverywhereItHasToBe:
    """Three places name the themes, and all three have to agree.

    `tokens.css` paints them, `theme.js` offers them in the picker, and a
    small inline script in `index.html` applies the remembered one before the
    first paint, because the module that owns it is deferred and the page
    would otherwise flash the default on every load.

    That inline script cannot import the module it is guarding against, so it
    carries its own copy of the list. A theme added to the other two and
    forgotten here does not fail: it is simply refused before paint, the
    default is applied instead, and the picker then quietly disagrees with the
    page for as long as anyone is looking at it.
    """

    def _named_in_theme_js(self) -> set[str]:
        source = THEME_JS.read_text(encoding="utf-8")
        block = source[source.index("export const THEMES"):source.index("];", source.index("export const THEMES"))]
        return set(re.findall(r'id:\s*"([a-z0-9-]+)"', block))

    def _allowed_before_paint(self) -> set[str]:
        return set(re.findall(r'stored === "([a-z0-9-]+)"', INDEX.read_text(encoding="utf-8")))

    def test_the_picker_offers_exactly_what_the_stylesheet_paints(self):
        assert self._named_in_theme_js() == set(_themes(_source()))

    def test_a_remembered_theme_is_applied_before_the_first_paint(self):
        assert self._allowed_before_paint() == self._named_in_theme_js(), (
            "the pre-paint script in index.html and THEMES disagree, so a theme "
            "is offered in the picker and refused on reload"
        )
