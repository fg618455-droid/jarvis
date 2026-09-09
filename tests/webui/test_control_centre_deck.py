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
import time

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


def open_panel(page, served: str, panel: str) -> None:
    """Go to a panel's address and wait until the view inside it is mounted.

    The panel is drawn the moment it opens and its body stays empty until the
    view module has been fetched, run, and asked its endpoint. Waiting for the
    panel alone reads the page a module load too early; the panel says when
    its contents have settled, so that is what to wait for.
    """
    page.goto(f"{served}/#/{panel}")
    page.wait_for_selector('.panel[aria-busy="false"]', timeout=20000)


def painted_deck(page, served: str) -> None:
    """Open the deck and wait until its widgets have a reading in them.

    A widget is built empty and filled from the first snapshot, and what is
    in one decides how tall it is. Measuring before that lands measures a
    rail of blank cards.
    """
    page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
    # Written on the conversation card by the first paint, and by nothing else.
    page.wait_for_selector(".widget[data-panel='conversation'][data-empty]", timeout=20000)


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

        open_panel(page, served, "tools")

        assert page.locator(".face-stage").is_visible(), "the face was replaced"
        assert page.locator(".deck").is_visible()
        assert not page.console_errors

    def test_every_panel_opens_over_a_deck_that_is_still_there(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")

        for panel in PANELS:
            open_panel(page, served, panel)
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
        page.goto(served, wait_until="domcontentloaded")
        open_panel(page, served, "tools")

        page.keyboard.press("Escape")
        page.wait_for_selector(".panel", state="detached", timeout=20000)

        assert page.locator(".panel").count() == 0
        assert page.evaluate("location.hash") == "#/deck"
        assert not page.console_errors

    def test_escape_in_a_field_is_left_to_the_field(self, page, served):
        """In a field, Escape belongs to the field.

        A key press there was never a departure, so it neither closes the
        panel nor raises the question about leaving one.
        """
        # The log's search box, because it is the one field in a panel that is
        # there whatever this machine happens to have configured.
        page.goto(served, wait_until="domcontentloaded")
        open_panel(page, served, "logs")
        page.locator(".panel input[type='search']").first.click()

        page.keyboard.press("Escape")
        # Long enough that a panel which was going to close would have.
        page.wait_for_timeout(400)

        assert page.locator(".panel").count() == 1, "an edit in progress was discarded"

    def test_closing_a_panel_returns_to_the_deck(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        open_panel(page, served, "tools")

        page.locator(".panel-close").click()
        page.wait_for_selector(".panel", state="detached", timeout=20000)

        assert page.locator(".panel").count() == 0
        assert page.evaluate("location.hash") == "#/deck"
        assert not page.console_errors


class TestTheDeckNeverGrowsThePage:
    """The deck is sized against the window, so nothing in it may grow it.

    A page that scrolls puts the face somewhere you have to scroll back to,
    which is the one thing this layout exists to prevent, and it is the rails
    that would do it: a card is as tall as the words in it, and the words
    depend on the reading, the language and the fonts the machine happens to
    have. A rail scrolls itself when what is in it does not fit. It is never
    allowed to make the deck taller instead.
    """

    def test_a_rail_of_tall_cards_scrolls_itself_rather_than_the_page(
        self, page, served,
    ):
        painted_deck(page, served)
        assert not page.evaluate(
            "() => document.documentElement.scrollHeight > innerHeight + 1"
        ), "the page scrolled before anything was even made tall"

        # Twice the room every card in both rails could ask for, which is the
        # shape of a longer reading, a longer language, or a taller font.
        page.evaluate(
            """() => {
                for (const card of document.querySelectorAll('.deck-rail .widget')) {
                    card.style.minHeight = '200px';
                }
            }"""
        )
        page.wait_for_timeout(200)

        measured = page.evaluate(
            """() => ({
                pageScrolls: document.documentElement.scrollHeight > innerHeight + 1,
                railScrolls: [...document.querySelectorAll('.deck-rail')]
                    .some((rail) => rail.scrollHeight > rail.clientHeight + 1),
            })"""
        )

        assert not measured["pageScrolls"], (
            "cards taller than the rail grew the page instead of scrolling the rail"
        )
        assert measured["railScrolls"], "the rail swallowed them without scrolling"


class TestOneSourceFailingIsNotTheDeckFailing:
    """Nine readings are fetched for the widgets, independently.

    A source that answers with an error already leaves its widget on nothing
    rather than blanking the rail. A source that simply never answers is the
    same fact and has to read the same way: a page whose widgets all wait for
    the slowest of nine is a page that shows nothing at all whenever one of
    them is a machine that is not at home.
    """

    def test_a_reading_that_never_answers_does_not_hold_up_the_others(
        self, page, served,
    ):
        # One of the nine the deck asks for together. Held rather than
        # refused, because a refusal is the case that already works.
        page.route("**/api/briefing*", lambda route: None)

        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(
            ".widget[data-panel='memory'] .num:not(:text('—'))", timeout=20000,
        )

        answered = page.evaluate(
            """() => ({
                memory: document.querySelector(
                    '.widget[data-panel="memory"] .num').textContent.trim(),
                exchange: document.querySelector(
                    '.widget[data-panel="conversation"]').dataset.empty,
                briefing: document.querySelector(
                    '.widget[data-panel="briefing"] .num').textContent.trim(),
            })"""
        )

        assert answered["memory"] not in ("", "—"), (
            "a widget whose own source answered is still empty"
        )
        assert answered["exchange"] in ("true", "false"), (
            "the deck never painted, so it never worked out what it holds"
        )
        assert answered["briefing"] == "—", (
            "the widget whose source said nothing invented a reading"
        )
        page.unroute_all(behavior="ignoreErrors")


class TestAPanelSaysWhenItsViewIsIn:
    """A panel exists before the view it holds does.

    Its head is drawn the moment it opens and its body stays empty until the
    view module has been fetched, run, and asked its endpoint. In between, a
    panel that says nothing about itself is indistinguishable from one whose
    view has arrived and had nothing to show: a screen reader announces the
    dialog and reads an empty box, and anything else looking at the page
    reads the same box and believes it.
    """

    def _slow_view(self, page, view, delay_sec):
        """Hold the view module back so the gap is long enough to look at."""
        def held(route):
            time.sleep(delay_sec)
            route.continue_()

        page.route(f"**/static/js/views/{view}.js", held)

    def test_a_panel_is_busy_until_its_view_has_mounted(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        self._slow_view(page, "tools", 1.5)

        page.goto(f"{served}/#/tools")
        page.wait_for_selector(".panel", state="visible", timeout=5000)

        # Open, named, and still empty: it has to say so rather than present
        # an empty body as the view's answer.
        assert page.locator(".panel-body .card").count() == 0, "the view arrived too fast"
        assert page.locator(".panel").get_attribute("aria-busy") == "true"

        page.wait_for_selector('.panel[aria-busy="false"]', timeout=15000)

        assert page.locator(".panel-body .card, .panel-body .empty").count(), (
            "the panel stopped saying it was busy before its view was in"
        )
        assert not page.console_errors
        page.unroute_all(behavior="ignoreErrors")

    def test_a_panel_whose_view_fails_stops_being_busy_too(self, page, served):
        """Otherwise a broken view leaves the page busy for ever."""
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        page.route("**/static/js/views/tools.js", lambda route: route.abort())

        page.goto(f"{served}/#/tools")
        page.wait_for_selector('.panel[aria-busy="false"]', timeout=15000)

        assert page.locator(".panel-body .empty").count() == 1, (
            "a view that never arrived left no explanation in its place"
        )


class TestARequestStopsWaitingEventually:
    """A connection that is accepted and then never answered is the case a
    view cannot see the end of on its own.

    A module that fails to load rejects, and a server that answers with an
    error rejects, and both of those already leave a reason in the panel. A
    socket that is open and silent does neither: the `await` inside the
    view's `mount` never settles, so the panel it was opened for stays
    `aria-busy` for as long as the tab is left open, announcing itself as
    still loading something that stopped arriving.

    How long the wait is depends on what was asked for, so both ends of that
    are asserted here: the short bound has to fire, and the long one has to
    not fire early.
    """

    def _deadlines(self, page, served) -> dict:
        """The bounds the page itself is built with, read from the page.

        Fast-forwarding by a number written here instead would pass whatever
        the module was changed to, because the test would be moving the
        clock by its own idea of the deadline rather than by the one the
        request is actually waiting on.
        """
        return page.evaluate(
            f"import('{served}/static/js/api.js').then(m => m.DEADLINES)",
        )

    def _never_answers(self, page, pattern) -> None:
        """Take the request and hold it: accepted, open, and silent."""
        page.route(pattern, lambda route: None)

    def test_a_panel_whose_endpoint_never_answers_stops_being_busy(
        self, page, served,
    ):
        page.clock.install()
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        deadlines = self._deadlines(page, served)
        self._never_answers(page, "**/api/tools")

        page.goto(f"{served}/#/tools")
        page.wait_for_selector(".panel", state="visible", timeout=5000)
        assert page.locator(".panel").get_attribute("aria-busy") == "true"

        page.clock.fast_forward(deadlines["reading"] + 1000)
        page.wait_for_selector('.panel[aria-busy="false"]', timeout=15000)

        assert page.locator(".panel-body .empty").count() == 1, (
            "the panel gave up waiting without saying why"
        )
        assert page.locator(".panel-body .empty").inner_text().strip(), (
            "the reason left in the panel is blank"
        )
        page.unroute_all(behavior="ignoreErrors")

    def test_a_reading_gives_up_long_before_work_the_user_asked_for_does(
        self, page, served,
    ):
        """The whole reason one bound will not do.

        A turn runs a model, a probe walks a chain of providers, and a
        briefing writes one: cutting those off at the bound that suits a
        reading would report a failure for work that was still running.
        """
        page.goto(served, wait_until="domcontentloaded")
        deadlines = self._deadlines(page, served)

        assert deadlines["work"] > deadlines["reading"], (
            "work the user asked for waits no longer than a reading does"
        )
        assert deadlines["reading"] > 0 and deadlines["work"] > 0, (
            "a bound of zero or less is not a bound"
        )

    def test_a_turn_is_not_cut_off_at_the_bound_a_reading_gets(
        self, page, served,
    ):
        page.clock.install()
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        deadlines = self._deadlines(page, served)
        self._never_answers(page, "**/api/chat")

        page.locator(".face-dock input[type='text']").fill("hello")
        page.keyboard.press("Enter")

        page.clock.fast_forward(deadlines["reading"] + 1000)
        page.wait_for_timeout(200)
        assert page.locator(".toast").count() == 0, (
            "a turn was reported as failed at the bound that suits a reading"
        )

        page.clock.fast_forward(deadlines["work"])
        page.wait_for_selector(".toast", timeout=15000)
        page.unroute_all(behavior="ignoreErrors")


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
        page.wait_for_selector('.panel[aria-busy="false"]', timeout=20000)

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
            open_panel(page, served, panel)
        page.goto(f"{served}/#/settings")
        page.wait_for_selector(".view-settings .settings-nav", state="visible", timeout=20000)

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
        painted_deck(page, served)

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

    def test_no_tile_sits_beside_a_gap(self, page, served):
        """A tile with empty space next to it reads as one that failed to
        load rather than as the end of the list."""
        self._deck(page, served)

        shape = page.evaluate(
            """() => {
                const tiles = document.querySelector('.widget-tiles');
                const row = Math.round(tiles.getBoundingClientRect().width);
                return {
                    row,
                    narrow: [...tiles.children]
                        .map((tile) => Math.round(tile.getBoundingClientRect().width))
                        .filter((width) => width < row - 2).length,
                    count: tiles.children.length,
                };
            }"""
        )

        assert shape["count"], "the rail carries no tiles at all"
        assert not shape["narrow"], (
            f"{shape['narrow']} of {shape['count']} tiles are narrower than the "
            f"{shape['row']}px rail they are in"
        )


class TestTheDeckFillsTheHeightItTakes:
    """The deck is sized against the window, so it has to fill it.

    Packed from the top, the rails left the bottom of the deck empty and the
    whole page read as top-weighted with a hole underneath. Distributing fixes
    that, but only if a card that has nothing to show refuses the room rather
    than growing into a tall empty box, which is the same hole with a border
    drawn round it.

    Both rails, at the widths where both are rails. Below 1240px the right one
    folds into a row under the deck and is a different component, so a
    measurement taken there says nothing about the one taken here.
    """

    def _deck(self, page, served):
        painted_deck(page, served)

    @pytest.mark.parametrize("rail", [".deck-rail-left", ".deck-rail-right"])
    def test_a_rail_of_readings_reaches_the_bottom_of_the_deck(
        self, page, served, rail,
    ):
        self._deck(page, served)

        empty = page.evaluate(
            """(selector) => {
                const rail = document.querySelector(selector);
                const kids = [...rail.children];
                const box = rail.getBoundingClientRect();
                const last = kids[kids.length - 1].getBoundingClientRect();
                return Math.round(box.bottom - last.bottom);
            }""",
            rail,
        )

        assert empty <= 4, f"{empty}px of {rail} is left empty under its last card"

    @pytest.mark.parametrize("rail", [".deck-rail-left", ".deck-rail-right"])
    def test_a_rail_leaves_no_hole_between_its_cards_either(
        self, page, served, rail,
    ):
        """Slack moved into the middle of a rail is the same slack.

        A rail that spaces its cards apart to reach the bottom passes the
        measurement above while reading exactly as it did: one block at the
        top, one at the foot, and the hole between them.
        """
        self._deck(page, served)

        biggest = page.evaluate(
            """(selector) => {
                const kids = [...document.querySelector(selector).children];
                let widest = 0;
                for (let n = 1; n < kids.length; n += 1) {
                    widest = Math.max(widest, Math.round(
                        kids[n].getBoundingClientRect().top
                        - kids[n - 1].getBoundingClientRect().bottom,
                    ));
                }
                return widest;
            }""",
            rail,
        )

        # The rail's own gap, and nothing more.
        assert biggest <= 24, f"a {biggest}px hole between two cards of {rail}"

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

    def test_the_exchange_takes_its_own_height_with_a_turn_or_without(
        self, page, served,
    ):
        """The failure this catches: an empty rail traded for an empty card.

        The exchange is three lines that do not wrap, so it is the same three
        lines tall whether it is showing a turn or saying there has not been
        one. Given a third of the rail either way it is a tall box holding a
        line of text, which is the hole the rail was packed to close with a
        border drawn round it.
        """
        self._deck(page, served)

        measured = page.evaluate(
            """() => {
                const card = document.querySelector('.widget[data-panel="conversation"]');
                const rail = document.querySelector('.deck-rail-right');
                const height = () => Math.round(card.getBoundingClientRect().height);
                const was = card.dataset.empty;
                const seen = {};

                for (const state of ['true', 'false']) {
                    card.dataset.empty = state;
                    void rail.offsetHeight;
                    seen[state] = height();
                }

                card.dataset.empty = was;
                return { ...seen, rail: Math.round(rail.getBoundingClientRect().height) };
            }"""
        )

        for state, taken in (("with no turn", measured["true"]), ("with one", measured["false"])):
            assert taken < measured["rail"] / 3, (
                f"the exchange {state} takes {taken}px of a "
                f"{measured['rail']}px rail"
            )
        assert measured["true"] == measured["false"], (
            "the rail is laid out differently for a card that is the same height, "
            "so the readings under it move the first time anyone speaks"
        )

    def test_a_tile_is_never_taller_than_a_card(self, page, served):
        """A tile carries less than a card, so it is never given more room.

        Measured against the cards beside it rather than against a number:
        both are a share of the same rail, so the one carrying one reading has
        to come out under the one carrying a reading and a line about it at
        every window this deck is used at.
        """
        self._deck(page, served)

        measured = page.evaluate(
            """() => ({
                tile: Math.max(...[...document.querySelectorAll('.widget-tile')]
                    .map((tile) => tile.getBoundingClientRect().height)),
                card: Math.min(...[...document.querySelectorAll(
                    '.deck-rail-left > .widget')]
                    .map((card) => card.getBoundingClientRect().height)),
            })"""
        )

        assert measured["tile"] <= measured["card"], (
            f"a tile grew to {round(measured['tile'])}px beside a "
            f"{round(measured['card'])}px card"
        )


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
