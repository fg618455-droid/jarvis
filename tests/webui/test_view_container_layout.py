"""A view lays itself out against the box it is in, not against the window.

The same view module is a full-width page at one address and a column inside
a 420px panel at another. A layout rule that asks the window how much room
there is answers with the window's width in both places, so the wide form of
a view is still applied inside a panel a third of that width: the columns
never collapse, the content runs past the panel's edge, and the scroll
container quietly cuts it off. Nothing throws, nothing logs, and the reading
is simply missing.

So the property is asserted where it fails: in a real browser, against a real
panel. Every panel is opened and nothing inside it is allowed to sit past the
view's right edge; then the same view is given a wide container in the same
window and has to take its wide form back. One window, two widths, two
layouts is the whole mechanism, and it is the only thing that distinguishes a
container query from the viewport query that looked identical at full width.
"""

from __future__ import annotations

import re
import socket
import threading
from pathlib import Path

import pytest

from jarvis.webui.server import WebUIConfig, WebUIMode, WebUIServer


CSS = Path(__file__).resolve().parents[2] / "src/jarvis/webui/static/css"

# Every panel that mounts a view module into the deck.
PANELS = [
    "conversation",
    "memory",
    "tools",
    "mcp",
    "system",
    "llm-routes",
    "logs",
    "crew",
    "security",
    "passive",
    "briefing",
]

# What sits past the right edge of the view, and how wide the view was.
OVERFLOW = """() => {
    const view = document.querySelector('.panel .view');
    if (!view) return null;
    const edge = view.getBoundingClientRect().right;
    const name = (node) => (
        typeof node.className === 'string'
            ? node.className
            : (node.className && node.className.baseVal) || node.tagName
    );
    const over = [];
    view.querySelectorAll('*').forEach((node) => {
        const box = node.getBoundingClientRect();
        // A hairline of rounding is not a clipped reading.
        if (box.width > 0 && box.right > edge + 1) {
            over.push(name(node).trim() + ' w=' + Math.round(box.width));
        }
    });
    return {width: Math.round(view.getBoundingClientRect().width), over};
}"""


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
    cfg = WebUIConfig(host="127.0.0.1", port=_free_port(), token="")
    server = WebUIServer(cfg)
    server.start()
    threading.Event().wait(0.5)
    yield cfg.url
    server.stop()


@pytest.fixture
def page(browser, served):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    opened = context.new_page()
    yield opened
    context.close()


def _open(page, served: str, panel: str) -> None:
    page.goto(f"{served}/#/{panel}", wait_until="domcontentloaded")
    page.wait_for_selector(".panel .view", state="visible", timeout=5000)
    page.wait_for_selector(".panel-body .card, .panel-body .empty", timeout=5000)
    page.wait_for_timeout(400)


def _widen_the_panel(page, width: str = "1180px") -> None:
    """Give the panel a wide container without touching the window.

    The window stays exactly as wide as it was, so anything that changes
    changed because the container did.
    """
    page.evaluate(
        "width => document.documentElement.style.setProperty('--panel', width)",
        width,
    )
    page.wait_for_timeout(400)


class TestAPanelShowsTheWholeView:
    @pytest.mark.parametrize("panel", PANELS)
    def test_nothing_is_cut_off_by_the_panel_edge(self, page, served, panel):
        _open(page, served, panel)

        reading = page.evaluate(OVERFLOW)

        assert reading is not None, f"{panel} mounted no view"
        assert not reading["over"], (
            f"{panel}: {len(reading['over'])} elements run past a "
            f"{reading['width']}px view: {reading['over'][:8]}"
        )


class TestTheContainerPicksTheLayout:
    """The same window, two container widths, two layouts."""

    def test_the_live_band_folds_in_a_panel_and_unfolds_in_a_wide_container(
        self, page, served
    ):
        columns = "() => getComputedStyle(document.querySelector('.voice')).gridTemplateColumns"

        _open(page, served, "conversation")
        narrow = page.evaluate(columns)
        _widen_the_panel(page)
        wide = page.evaluate(columns)

        assert len(narrow.split()) == 1, (
            f"three readings side by side in a panel: {narrow}"
        )
        assert len(wide.split()) == 3, (
            f"the wide form did not come back in a wide container: {wide}"
        )

    def test_a_table_stacks_in_a_panel_and_is_a_table_in_a_wide_container(
        self, page, served
    ):
        """A column pushed off the edge is not a column, so it stops being one.

        Comparing values down a page is what a table is for, and a container
        too narrow to hold the columns is not offering the comparison. Each
        row becomes a labelled record there, and every reading stays legible.
        """
        shape = """() => {
            const cell = document.querySelector('.panel .view tbody td');
            const row = cell.closest('tr');
            const column = cell.closest('table').querySelector('thead th');
            return {
                cells: row.querySelectorAll('td').length,
                stacked: cell.getBoundingClientRect().width
                    > row.getBoundingClientRect().width * 0.9,
                // The rendered label, and the column name it has to match.
                // An absent `data-label` renders as the empty string, which
                // is a cell that looks labelled to a loose check and carries
                // no reading at all.
                labelled: getComputedStyle(cell, '::before').content,
                column: column && column.textContent.trim(),
            };
        }"""

        _open(page, served, "tools")
        narrow = page.evaluate(shape)
        _widen_the_panel(page)
        wide = page.evaluate(shape)

        assert narrow["stacked"], "a five-column table is still five columns in a panel"
        assert narrow["column"], "the tools table rendered no columns to be labelled by"
        assert narrow["column"] in narrow["labelled"], (
            "a stacked cell does not carry its column name, so the value has "
            f"lost the question it answers: {narrow['labelled']!r}"
        )
        assert not wide["stacked"], "the table did not come back in a wide container"
        assert wide["cells"] == narrow["cells"], "a stacked table dropped a reading"


class TestViewLayoutIsAskedOfTheContainer:
    """The stylesheet rule behind it, so the direction survives the next edit.

    A view-layout rule written as a viewport query is invisible at full width
    and wrong in every panel, which is exactly the shape of fault that gets
    reintroduced by someone adding one more breakpoint.
    """

    def _source(self, name: str) -> str:
        return (CSS / name).read_text(encoding="utf-8")

    def test_a_view_declares_a_query_container(self):
        hosts = re.findall(
            r"^(\.[a-z-]+)\s*\{[^}]*container(?:-type)?\s*:[^;]*inline-size",
            self._source("app.css") + self._source("deck.css"),
            re.MULTILINE | re.DOTALL,
        )

        assert ".view" in hosts, "a view has no container to ask about its width"
        assert ".panel-body" in hosts, "a panel body has no container"

    def test_the_view_layer_holds_no_viewport_breakpoints(self):
        """Sizes chosen against the window belong to the shell, not to a view."""
        offenders = re.findall(r"@media[^{]*(?:max|min)-width[^{]*", self._source("views.css"))

        assert not offenders, f"views.css asks the window how wide a view is: {offenders}"

    def test_the_view_layer_asks_a_container_instead(self):
        assert re.search(r"@container\s+view\s*\(", self._source("views.css")), (
            "views.css has no container query, so nothing reshapes a narrow view"
        )
