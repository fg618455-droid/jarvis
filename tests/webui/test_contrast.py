"""Contrast is measured in a browser, not promised in a review.

A palette is the one part of an interface whose correctness is a number, and
the number is invisible from the source: a token is legible or not only once
it is painted on whatever surface it actually lands on, at whatever size the
rule that used it chose. Reading `tokens.css` cannot tell you that
`--fg-mute` carries 11px widget titles onto three different surfaces.

So the check renders the real interface and measures what a reader sees. It
is written as a mechanism rather than as a table of expected values, which
means it holds for tokens and themes that do not exist yet: whatever a future
theme paints, it is held to the same floor as the ones here.

Two floors, both from WCAG 2.2:

- text against the surface behind it: 4.5:1, or 3:1 once the text is large
  (24px, or 18.66px when bold)
- a focus indicator against what sits next to it: 3:1

The second one exists because a rule setting `outline: none` on a field
outranks a global `:focus-visible` rule and removes the ring everywhere at
once, silently, in a way no view test notices: the page still renders, still
answers, and simply cannot be navigated by keyboard.
"""

from __future__ import annotations

import socket
import threading

import pytest

from jarvis.webui.server import WebUIConfig, WebUIMode, WebUIServer


# Everything the deck can open, plus the destination that replaces it.
VIEWS = [
    "deck",
    "memory",
    "conversation",
    "passive",
    "tools",
    "mcp",
    "briefing",
    "security",
    "system",
    "settings",
    "llm-routes",
    "logs",
    "crew",
]

# Read from `theme.js` rather than listed, so a theme added there is measured
# here without anyone remembering to add it.
THEMES_FROM_SOURCE = """() => {
    return fetch('/static/js/theme.js')
        .then((r) => r.text())
        .then((source) => [...source.matchAll(/id:\\s*"([a-z0-9-]+)"/g)].map((m) => m[1]));
}"""


# Shared measuring instrument. Colours are resolved by painting them into a
# canvas, because a computed style may still be `oklch(...)` and every theme
# here is authored in it.
COLOUR_JS = """
const _cv = document.createElement('canvas');
_cv.width = _cv.height = 1;
const _ctx = _cv.getContext('2d', { willReadFrequently: true });

function srgb(value) {
    _ctx.clearRect(0, 0, 1, 1);
    _ctx.fillStyle = '#000000';
    _ctx.fillStyle = value;
    _ctx.fillRect(0, 0, 1, 1);
    const d = _ctx.getImageData(0, 0, 1, 1).data;
    return [d[0] / 255, d[1] / 255, d[2] / 255];
}

function alphaOf(value) {
    const m = String(value).match(/^rgba?\\([^)]*?,\\s*([\\d.]+)\\s*\\)$/);
    if (m) return parseFloat(m[1]);
    const slash = String(value).match(/\\/\\s*([\\d.]+%?)\\s*\\)/);
    if (slash) {
        const raw = slash[1];
        return raw.endsWith('%') ? parseFloat(raw) / 100 : parseFloat(raw);
    }
    if (String(value) === 'transparent') return 0;
    return 1;
}

function luminance(value) {
    const lin = (u) => (u <= 0.04045 ? u / 12.92 : Math.pow((u + 0.055) / 1.055, 2.4));
    const [r, g, b] = srgb(value).map(lin);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(a, b) {
    const l1 = luminance(a);
    const l2 = luminance(b);
    const hi = Math.max(l1, l2);
    const lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
}

/* What is really behind a node. A background is only a reading once it is
   opaque, so a translucent fill is composited onto whatever it sits on and
   the walk carries on upwards. */
function effectiveBackground(node) {
    let composite = null;
    let current = node;
    while (current && current !== document.documentElement.parentNode) {
        const style = getComputedStyle(current);
        const colour = style.backgroundColor;
        const a = alphaOf(colour);
        if (a > 0) {
            const [r, g, b] = srgb(colour);
            if (composite === null) composite = { r, g, b, a };
            else {
                const out = composite.a + a * (1 - composite.a);
                composite = {
                    r: (composite.r * composite.a + r * a * (1 - composite.a)) / out,
                    g: (composite.g * composite.a + g * a * (1 - composite.a)) / out,
                    b: (composite.b * composite.a + b * a * (1 - composite.a)) / out,
                    a: out,
                };
            }
            if (composite.a >= 0.999) break;
        }
        current = current.parentElement;
    }
    if (composite === null) return 'rgb(0, 0, 0)';
    const to255 = (v) => Math.round(Math.min(1, Math.max(0, v)) * 255);
    return `rgb(${to255(composite.r)}, ${to255(composite.g)}, ${to255(composite.b)})`;
}

function describe(node) {
    const name = node.getAttribute('aria-label')
        || node.id
        || (typeof node.className === 'string' ? node.className : '')
        || node.tagName;
    return `${node.tagName.toLowerCase()}[${String(name).slice(0, 48)}]`;
}
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            launched = driver.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure is a skip
            pytest.skip(f"chromium is not available: {exc}")
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def served() -> str:
    cfg = WebUIConfig(
        host="127.0.0.1", port=_free_port(), token="", mode=WebUIMode.STANDALONE,
    )
    server = WebUIServer(cfg)
    server.start()
    threading.Event().wait(0.5)
    yield cfg.url
    server.stop()


@pytest.fixture
def page(browser, served):
    context = browser.new_context()
    opened = context.new_page()
    yield opened
    context.close()


def _painted(page, view: str) -> None:
    """Wait until the view is in the page and the stylesheet has applied.

    Measuring early is the one way this whole file can lie, in two ways.
    Before the cascade is in place every element reports no focus ring and no
    colour, and the sweep reads that as an interface with no contrast anywhere
    rather than as a page that has not finished loading. And before the view
    is mounted there is nothing of it to sweep: the panel is drawn while its
    module is still being fetched, so a sweep of "the page" measures the deck
    behind it and reports the view as clean.
    """
    if view == "deck":
        page.wait_for_selector(".widget[data-panel='conversation'][data-empty]", timeout=20000)
    elif view == "settings":
        page.wait_for_selector(".view-settings .settings-nav", state="visible", timeout=20000)
    else:
        page.wait_for_selector('.panel[aria-busy="false"]', timeout=20000)
    page.wait_for_function(
        """() => {
            const accent = getComputedStyle(document.documentElement)
                .getPropertyValue('--accent').trim();
            return accent.length > 0;
        }""",
        timeout=20000,
    )


def _themes(page, served) -> list[str]:
    page.goto(served, wait_until="domcontentloaded")
    found = page.evaluate(THEMES_FROM_SOURCE)
    assert found, "no themes parsed out of theme.js"
    return found


# Every element that can hold focus, in the order the document offers them.
FOCUS_SWEEP = COLOUR_JS + """
() => {
    const focusable = [...document.querySelectorAll(
        'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )].filter((node) => {
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && !node.disabled;
    });

    return focusable.map((node) => {
        node.focus();
        const style = getComputedStyle(node);
        const width = parseFloat(style.outlineWidth) || 0;
        const visible = style.outlineStyle !== 'none' && width > 0;
        // The ring is drawn outside the element, so it lands on whatever the
        // element sits on as well as on the element itself. It has to be
        // legible against both, so the weaker of the two is the reading.
        const against = Math.min(
            ratio(style.outlineColor, effectiveBackground(node)),
            ratio(style.outlineColor, effectiveBackground(node.parentElement || node)),
        );
        return {
            what: describe(node),
            focusVisible: node.matches(':focus-visible'),
            outlineStyle: style.outlineStyle,
            outlineWidth: width,
            visible,
            ratio: Math.round(against * 100) / 100,
        };
    });
}"""


# Every element carrying text of its own, measured where it is painted.
TEXT_SWEEP = COLOUR_JS + """
(theme) => {
    document.documentElement.dataset.theme = theme;
    // A forced reflow, so the measurements below read the new paint rather
    // than the one the attribute just replaced.
    void document.body.offsetHeight;

    const findings = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const seen = new Set();

    while (walker.nextNode()) {
        const text = walker.currentNode.textContent.trim();
        if (!text) continue;
        const node = walker.currentNode.parentElement;
        if (!node || seen.has(node)) continue;
        seen.add(node);

        const box = node.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;

        const style = getComputedStyle(node);
        if (style.visibility === 'hidden' || style.display === 'none') continue;
        // A disabled control is exempt: it is telling you it cannot be used,
        // and dimming is how it says so.
        if (node.closest(':disabled, [aria-disabled="true"]')) continue;
        if (parseFloat(style.opacity) < 0.999) continue;

        const size = parseFloat(style.fontSize);
        const weight = parseInt(style.fontWeight, 10) || 400;
        const large = size >= 24 || (size >= 18.66 && weight >= 700);
        const floor = large ? 3.0 : 4.5;

        const behind = effectiveBackground(node);
        const measured = ratio(style.color, behind);
        if (measured + 0.005 < floor) {
            findings.push({
                what: describe(node),
                text: text.slice(0, 40),
                size,
                colour: style.color,
                behind,
                ratio: Math.round(measured * 100) / 100,
                floor,
            });
        }
    }
    return findings;
}"""


class TestTextClearsItsSurfaceInEveryTheme:
    """The floor every reading is held to, wherever it is painted.

    This is written against what the browser renders rather than against the
    token file, because a token is legible or not only once something has
    chosen a surface for it and a size to set it at. `--fg-mute` reads
    perfectly well as a name in `tokens.css` and carries 11px widget titles
    onto three different surfaces in practice.

    Themes are read out of `theme.js`, so a palette added there is measured
    here without anyone remembering to extend a list.
    """

    def test_every_theme_is_measured(self, page, served):
        """A sweep that silently found no themes would pass forever."""
        assert len(_themes(page, served)) >= 2

    @pytest.mark.parametrize("view", VIEWS)
    def test_no_reading_falls_under_its_floor(self, page, served, view):
        page.goto(f"{served}/#/{view}", wait_until="domcontentloaded")
        _painted(page, view)

        failures = {}
        for theme in page.evaluate(THEMES_FROM_SOURCE):
            found = page.evaluate(TEXT_SWEEP, theme)
            if found:
                failures[theme] = [
                    f"{f['what']} {f['ratio']}:1 needs {f['floor']} "
                    f"({f['colour']} on {f['behind']}, {f['size']}px)"
                    for f in found
                ]

        assert not failures, f"{view}: {failures}"


class TestEveryFocusableThingSaysWhereFocusIs:
    """A keyboard user has to be able to see where they are.

    The failure this catches is specific and quiet: a rule that sets
    `outline: none` on fields outranks the global `:focus-visible` ring, so
    the element still reports that it wants a visible ring and is not given
    one. Nothing throws and nothing looks wrong until you put the mouse down.
    """

    @pytest.mark.parametrize("view", ["deck", "settings", "mcp", "llm-routes", "logs"])
    def test_a_focused_control_is_visibly_ringed(self, page, served, view):
        page.goto(f"{served}/#/{view}", wait_until="domcontentloaded")
        _painted(page, view)

        results = page.evaluate(FOCUS_SWEEP)
        assert results, f"{view} offered nothing focusable"

        unringed = [r for r in results if r["focusVisible"] and not r["visible"]]

        assert not unringed, (
            f"{view}: these ask for a focus ring and are not given one: "
            f"{[r['what'] for r in unringed]}"
        )

    @pytest.mark.parametrize("view", ["deck", "settings", "mcp", "llm-routes", "logs"])
    def test_the_ring_is_legible_against_what_it_sits_on(self, page, served, view):
        page.goto(f"{served}/#/{view}", wait_until="domcontentloaded")
        _painted(page, view)

        results = page.evaluate(FOCUS_SWEEP)
        faint = [
            (r["what"], r["ratio"])
            for r in results
            if r["focusVisible"] and r["visible"] and r["ratio"] < 3.0
        ]

        assert not faint, f"{view}: focus rings under 3:1: {faint}"
