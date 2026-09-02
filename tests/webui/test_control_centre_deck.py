"""The deck: one face, widgets around it, and a single way out.

The control centre is one place rather than twelve. The face is the page
rather than a destination inside it, every reading is a widget around that
face, and the only thing that replaces the whole view is Settings. A detail
that needs room opens beside the face instead of instead of it.

These are the structural properties that redesign is made of, so they are
asserted against a real browser: a layout rule that does not hold is
invisible to an API test and returns 200 all the same.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from jarvis.webui.server import WebUIConfig, WebUIMode, WebUIServer


# Every widget that opens a detail panel, by the address that panel answers to.
PANELS = [
    "conversation",
    "memory",
    "tools",
    "mcp",
    "security",
    "system",
    "llm-routes",
    "logs",
    "passive",
    "crew",
    "briefing",
]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            launched = driver.chromium.launch(args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
            ])
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
    context = browser.new_context()
    opened = context.new_page()
    opened.console_errors = []
    opened.foreign_requests = []
    opened.on("console", lambda message: (
        opened.console_errors.append(f"{message.type}: {message.text}")
        if message.type in ("error", "warning") else None
    ))
    opened.on("pageerror", lambda error: opened.console_errors.append(f"pageerror: {error}"))
    opened.on("request", lambda request: (
        opened.foreign_requests.append(request.url)
        if not request.url.startswith(served) else None
    ))
    yield opened
    context.close()


class TestTheFaceIsThePage:
    def test_the_deck_is_where_the_interface_opens(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".deck", state="visible", timeout=5000)

        assert page.evaluate("location.hash") in ("", "#/deck")
        assert page.locator(".face-stage").is_visible()
        assert not page.console_errors

    def test_the_face_is_still_there_with_a_panel_open(self, page, served):
        """A detail opens beside the face rather than instead of it."""
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")

        page.goto(f"{served}/#/tools")
        page.wait_for_selector(".panel", state="visible", timeout=5000)

        assert page.locator(".face-stage").is_visible(), "the face was replaced"
        assert page.locator(".deck").is_visible()
        assert not page.console_errors

    def test_every_panel_opens_over_a_deck_that_is_still_there(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")

        for panel in PANELS:
            page.goto(f"{served}/#/{panel}")
            page.wait_for_selector(".panel", state="visible", timeout=5000)
            page.wait_for_timeout(250)
            assert page.locator(".panel-title").inner_text().strip(), f"{panel} has no heading"
            # The panel names itself; the view inside it having rendered
            # something is the separate fact worth checking.
            assert page.locator(".panel-body .card, .panel-body .empty").count(), (
                f"{panel} opened but mounted nothing"
            )
            assert page.locator(".face-stage").count() == 1, f"{panel} replaced the face"
            assert not page.console_errors, f"{panel}: {page.console_errors}"

    def test_escape_closes_the_panel(self, page, served):
        """It calls itself a dialog, so it answers to the dialog key."""
        page.goto(f"{served}/#/tools", wait_until="domcontentloaded")
        page.wait_for_selector(".panel", state="visible")
        page.wait_for_timeout(300)

        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        assert page.locator(".panel").count() == 0
        assert page.evaluate("location.hash") == "#/deck"
        assert not page.console_errors

    def test_escape_in_a_field_is_left_to_the_field(self, page, served):
        """Reflex should not discard an edit nobody has saved.

        The MCP and route editors hold typed changes with no warning of their
        own, so the one key a person presses without thinking must not be the
        one that throws them away.
        """
        # The log's search box, because it is the one field in a panel that is
        # there whatever this machine happens to have configured.
        page.goto(f"{served}/#/logs", wait_until="domcontentloaded")
        page.wait_for_selector(".panel", state="visible")
        page.wait_for_selector(".panel input[type='search']", timeout=8000)
        page.locator(".panel input[type='search']").first.click()

        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        assert page.locator(".panel").count() == 1, "an edit in progress was discarded"

    def test_closing_a_panel_returns_to_the_deck(self, page, served):
        page.goto(f"{served}/#/tools", wait_until="domcontentloaded")
        page.wait_for_selector(".panel", state="visible")

        page.locator(".panel-close").click()
        page.wait_for_timeout(300)

        assert page.locator(".panel").count() == 0
        assert page.evaluate("location.hash") == "#/deck"
        assert not page.console_errors


class TestOneSettingsButton:
    def test_settings_is_the_only_separate_destination(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".deck", state="visible")

        assert page.locator(".settings-button").count() == 1
        # The old parallel sidebar of independent pages is gone.
        assert page.locator(".sidebar").count() == 0
        assert page.locator(".nav-group").count() == 0
        assert page.locator(".nav-item").count() == 0

    def test_settings_replaces_the_deck_rather_than_opening_beside_it(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".deck", state="visible")

        page.locator(".settings-button").click()
        page.wait_for_selector(".view-settings", state="visible", timeout=5000)

        assert page.evaluate("location.hash") == "#/settings"
        assert page.locator(".deck").count() == 0
        assert not page.console_errors

    def test_leaving_settings_comes_back_to_the_deck(self, page, served):
        page.goto(f"{served}/#/settings", wait_until="domcontentloaded")
        page.wait_for_selector(".view-settings", state="visible")

        page.locator(".settings-back").click()
        page.wait_for_selector(".deck", state="visible", timeout=5000)

        assert page.evaluate("location.hash") == "#/deck"
        assert not page.console_errors


class TestWidgetsLeadToTheirDetail:
    def test_a_widget_opens_the_panel_that_holds_its_detail(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".widget", state="visible")

        page.locator('.widget[data-panel="tools"] .widget-open').click()
        page.wait_for_selector(".panel", state="visible", timeout=5000)

        assert page.evaluate("location.hash") == "#/tools"
        assert not page.console_errors

    def test_the_deck_carries_a_widget_for_every_panel_it_can_open(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".widget", state="visible")

        panels = page.locator(".widget[data-panel]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.panel)"
        )

        assert set(panels) == set(PANELS), "a panel is unreachable from the deck"


class TestTheOfflineRuleSurvivesTheRedesign:
    def test_nothing_is_fetched_from_outside_the_server(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        for panel in PANELS:
            page.goto(f"{served}/#/{panel}")
            page.wait_for_timeout(250)
        page.goto(f"{served}/#/settings")
        page.wait_for_timeout(400)

        assert not page.foreign_requests, f"outbound: {page.foreign_requests}"


class TestTheThemeIsPicked:
    def _themes(self, page):
        return page.locator("#theme option").evaluate_all(
            "options => options.map(option => option.value)"
        )

    def test_the_picker_offers_every_theme_the_stylesheet_defines(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector("#theme", state="visible")

        offered = set(self._themes(page))
        defined = set(page.evaluate(
            """async () => {
                const { THEMES } = await import('/static/js/theme.js');
                return THEMES.map(theme => theme.id);
            }"""
        ))

        assert offered == defined
        assert "graphite" in offered, "the established look is not on offer"
        assert len(offered) >= 2

    def test_choosing_a_theme_repaints_the_page(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector("#theme", state="visible")
        before = page.evaluate(
            "() => getComputedStyle(document.body).backgroundColor"
        )

        other = [name for name in self._themes(page) if name != page.evaluate(
            "() => document.documentElement.dataset.theme"
        )][0]
        page.select_option("#theme", other)
        page.wait_for_timeout(300)

        assert page.evaluate("() => document.documentElement.dataset.theme") == other
        assert page.evaluate(
            "() => getComputedStyle(document.body).backgroundColor"
        ) != before
        assert not page.console_errors

    def test_a_chosen_theme_survives_a_reload(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector("#theme", state="visible")
        other = [name for name in self._themes(page) if name != page.evaluate(
            "() => document.documentElement.dataset.theme"
        )][0]
        page.select_option("#theme", other)
        page.wait_for_timeout(200)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#theme", state="visible")

        assert page.evaluate("() => document.documentElement.dataset.theme") == other
        assert page.locator("#theme").input_value() == other


class TestMotionStaysOptional:
    def test_a_reader_who_asked_for_less_motion_gets_none(self, browser, served):
        context = browser.new_context(reduced_motion="reduce")
        opened = context.new_page()
        try:
            opened.goto(served, wait_until="domcontentloaded")
            opened.wait_for_selector(".face-stage", state="visible")
            moving = opened.evaluate(
                """() => [...document.querySelectorAll('*')]
                    .map(node => getComputedStyle(node).animationName)
                    .filter(name => name && name !== 'none')"""
            )
            assert moving == [], f"still animating: {sorted(set(moving))}"
        finally:
            context.close()


class TestARailReadsAsReadingsRatherThanAsAForm:
    """Three ways a rail stops looking like what it is.

    A widget is a card with a heading and one short reading under it. The
    reading is often a status chip, which is sized by its word: stretched to
    the width of the rail it becomes a bordered box spanning the card, and a
    bordered box spanning a card is the shape of an empty text input. A name
    clipped by two pixels reads as a different, shorter name. A last tile
    beside a gap reads as a tile that failed to load.
    """

    def _deck(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".widget-tiles", state="visible", timeout=5000)
        page.wait_for_timeout(400)

    def test_a_status_chip_is_as_wide_as_its_word(self, page, served):
        self._deck(page, served)

        stretched = page.evaluate(
            """() => [...document.querySelectorAll('.widget .chip')].filter((chip) => {
                const card = chip.closest('.widget').getBoundingClientRect();
                // Padding aside, a chip filling its card is a chip that was
                // stretched rather than one with a very long word in it.
                return chip.getBoundingClientRect().width > card.width * 0.85;
            }).map((chip) => chip.textContent.trim())"""
        )

        assert stretched == [], f"chips stretched into input-shaped boxes: {stretched}"

    def test_a_widget_shows_its_whole_name(self, page, served):
        self._deck(page, served)

        clipped = page.evaluate(
            """() => [...document.querySelectorAll('.widget-title')]
                .filter((title) => title.scrollWidth > title.clientWidth + 1)
                .map((title) => title.textContent.trim())"""
        )

        assert clipped == [], f"widget names cut off by their own tile: {clipped}"

    def test_an_odd_last_tile_takes_the_width_rather_than_leaving_a_gap(
        self, page, served
    ):
        self._deck(page, served)

        shape = page.evaluate(
            """() => {
                const tiles = document.querySelector('.widget-tiles');
                return {
                    count: tiles.children.length,
                    row: Math.round(tiles.getBoundingClientRect().width),
                    last: Math.round(
                        tiles.lastElementChild.getBoundingClientRect().width
                    ),
                };
            }"""
        )

        if shape["count"] % 2 == 0:
            pytest.skip("an even number of tiles leaves no gap to fill")
        assert shape["last"] == pytest.approx(shape["row"], abs=2), (
            f"the last of {shape['count']} tiles is {shape['last']}px in a "
            f"{shape['row']}px row, so it sits beside a gap"
        )


class TestTheDeckFillsTheHeightItTakes:
    """The deck is sized against the window, so it has to fill it.

    Packed from the top, the rails left the bottom of the deck empty and the
    whole page read as top-weighted with a hole underneath. Distributing fixes
    that, but only if a card that has nothing to show refuses the room rather
    than growing into a tall empty box, which is the same hole with a border
    drawn round it.
    """

    def _deck(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".widget-tiles", state="visible", timeout=5000)
        page.wait_for_timeout(500)

    def test_a_rail_of_readings_reaches_the_bottom_of_the_deck(self, page, served):
        self._deck(page, served)

        left = page.evaluate(
            """() => {
                const rail = document.querySelector('.deck-rail-left');
                const cards = [...rail.querySelectorAll(':scope > .widget')];
                const last = cards[cards.length - 1].getBoundingClientRect();
                return Math.round(rail.getBoundingClientRect().bottom - last.bottom);
            }"""
        )

        assert left <= 4, f"{left}px of the left rail is left empty under its last card"

    def test_the_dock_stands_on_the_floor_of_the_stage(self, page, served):
        self._deck(page, served)

        below = page.evaluate(
            """() => {
                const stage = document.querySelector('.face-stage').getBoundingClientRect();
                const dock = document.querySelector('.face-dock').getBoundingClientRect();
                return Math.round(stage.bottom - dock.bottom);
            }"""
        )

        # The stage's own padding, and nothing more.
        assert below <= 32, f"{below}px of dead air under the dock"

    def test_a_card_with_nothing_to_show_does_not_take_the_room(self, page, served):
        """The failure this catches: an empty rail traded for an empty card."""
        self._deck(page, served)

        measured = page.evaluate(
            """() => {
                const card = document.querySelector('.widget[data-panel="conversation"]');
                const rail = document.querySelector('.deck-rail-right');
                const height = () => Math.round(card.getBoundingClientRect().height);
                const was = card.dataset.empty;

                card.dataset.empty = 'true';
                void rail.offsetHeight;
                const empty = height();

                card.dataset.empty = 'false';
                void rail.offsetHeight;
                const full = height();

                card.dataset.empty = was;
                return { empty, full, rail: Math.round(rail.getBoundingClientRect().height) };
            }"""
        )

        assert measured["empty"] < measured["rail"] / 3, (
            f"an empty card takes {measured['empty']}px of a "
            f"{measured['rail']}px rail"
        )
        assert measured["full"] > measured["empty"], (
            "the card does not grow even when it has a turn to show"
        )

    def test_the_tiles_are_never_stretched(self, page, served):
        """A tile is one number and its name; three times that height is a box."""
        self._deck(page, served)

        tallest = page.evaluate(
            """() => Math.max(...[...document.querySelectorAll('.widget-tile')]
                .map(tile => tile.getBoundingClientRect().height))"""
        )

        assert tallest < 120, f"a tile grew to {round(tallest)}px"


class TestAStatusChipIsTonedByWhatItSays:
    """A chip's colour is part of its text, not decoration beside it.

    The security widget carries two facts that are true at different times:
    which level is in force, and whether anything is waiting for an answer.
    Toning the level by the waiting count merges them, so a gate that is
    switched off reads in the reassuring tone whenever nothing happens to be
    queued, which is exactly when nobody is looking closely.
    """

    def _with_security(self, page, served, payload):
        page.route(
            "**/api/security",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            ),
        )
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".deck-rail-left .widget", state="visible", timeout=5000)
        page.wait_for_function(
            """() => [...document.querySelectorAll('.deck-rail-left .widget')].some(
                (node) => node.dataset.panel === 'security'
                    && node.querySelector('.chip')
            )""",
            timeout=5000,
        )
        return page.evaluate(
            """() => {
                const widget = document.querySelector('.widget[data-panel="security"]');
                return [...widget.querySelectorAll('.chip')].map((chip) => ({
                    text: chip.textContent.trim(),
                    tone: [...chip.classList].filter((name) => name !== 'chip'),
                }));
            }"""
        )

    @pytest.mark.parametrize(
        "level,expected",
        [("critical", "ok"), ("paranoid", "ok"), ("off", "warn")],
    )
    def test_the_level_chip_is_toned_by_the_level(self, page, served, level, expected):
        chips = self._with_security(
            page, served, {"level": level, "levels": [], "pending": [], "log": []},
        )

        named = [chip for chip in chips if chip["text"] == level]
        assert named, f"no chip carries the level {level!r}: {chips}"
        assert named[0]["tone"] == [expected], (
            f"level {level!r} is painted {named[0]['tone']} rather than {expected!r}"
        )

    def test_a_gate_that_is_off_says_so_even_with_nothing_queued(self, page, served):
        """The failure this catches: `off` with an empty queue reading as `ok`."""
        chips = self._with_security(
            page, served, {"level": "off", "levels": [], "pending": [], "log": []},
        )

        assert "ok" not in [tone for chip in chips for tone in chip["tone"]], (
            f"a disabled security gate is painted as healthy: {chips}"
        )

    def test_something_waiting_keeps_its_own_chip(self, page, served):
        """The waiting count is a second fact and needs a second object.

        Toning the level chip by it was the only thing carrying it in colour,
        so moving the level onto its own meaning must not drop the signal.
        """
        chips = self._with_security(
            page,
            served,
            {
                "level": "critical",
                "levels": [],
                "pending": [{"id": "a"}, {"id": "b"}],
                "log": [],
            },
        )

        waiting = [chip for chip in chips if "2" in chip["text"]]
        assert waiting, f"nothing on the widget says two decisions are waiting: {chips}"
        assert waiting[0]["tone"] == ["warn"], (
            f"a queued decision is painted {waiting[0]['tone']} rather than 'warn'"
        )


class TestAWidgetReadsTheSameSourceAsItsPanel:
    """A widget is the compact version of its panel, so it has to agree with it.

    The two are fed by different endpoints, and a widget wired to the wrong
    one does not fail: it reads a field the payload does not carry, falls back
    to its default, and shows a confident zero next to a panel showing five.
    Nothing throws and nothing logs, so the only thing that catches it is
    asserting the number the widget paints against the number its panel is
    built from.
    """

    def test_the_memory_widget_counts_the_nodes_its_panel_counts(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".deck-rail-left .widget", state="visible", timeout=5000)
        # Wait for the reading rather than for a length of time. A widget that
        # has not been painted yet still shows the em dash it starts with, and
        # stripping the non-digits out of that leaves an empty string that
        # reads as a confident zero: the exact failure this test exists to
        # catch, arriving as a false one under load.
        page.wait_for_function(
            """() => {
                const widget = document.querySelector('.widget[data-panel="memory"]');
                const num = widget && widget.querySelector('.num');
                return num && /\\d/.test(num.textContent);
            }""",
            timeout=8000,
        )

        graphed = page.evaluate(
            """async () => {
                const response = await fetch('/api/graph/stats');
                return (await response.json()).total_nodes;
            }"""
        )
        painted = page.evaluate(
            r"""() => {
                const widget = [...document.querySelectorAll('.deck-rail-left .widget')]
                    .find((node) => node.querySelector('.widget-title')
                        .textContent.trim().toLowerCase().includes('memory'));
                return Number(widget.querySelector('.num').textContent.replace(/\D/g, ''));
            }"""
        )

        assert painted == graphed, (
            f"the rail says {painted} nodes where the graph has {graphed}"
        )
