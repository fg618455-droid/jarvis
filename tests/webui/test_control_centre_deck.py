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


class TestTheFaceIsEditableOnItsOwn:
    def test_the_face_is_chosen_from_this_interface(self, page, served):
        """Picking a face is a control here rather than a page inside the frame."""
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")

        page.locator(".face-settings-open").click()
        page.wait_for_selector(".face-settings", state="visible", timeout=5000)

        faces = page.locator(".face-settings [name='face'] option").evaluate_all(
            "options => options.map(option => option.value)"
        )
        assert "board" in faces and "neural" in faces

    def test_choosing_a_face_loads_that_face(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        page.locator(".face-settings-open").click()
        page.wait_for_selector(".face-settings", state="visible")

        page.select_option(".face-settings [name='face']", "radial")
        page.wait_for_timeout(400)

        assert "faces/radial" in page.locator(".face-frame").get_attribute("src")
        assert not page.console_errors

    def test_the_chosen_face_survives_a_reload(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        page.locator(".face-settings-open").click()
        page.wait_for_selector(".face-settings", state="visible")
        page.select_option(".face-settings [name='face']", "neural")
        page.wait_for_timeout(300)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".face-frame", state="visible")

        assert "faces/neural" in page.locator(".face-frame").get_attribute("src")

    def test_the_face_takes_its_size_from_this_interface(self, page, served):
        page.goto(served, wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage", state="visible")
        page.locator(".face-settings-open").click()
        page.wait_for_selector(".face-settings", state="visible")
        before = page.locator(".face-frame").bounding_box()["width"]

        page.fill(".face-settings [name='size']", "420")
        page.dispatch_event(".face-settings [name='size']", "input")
        page.wait_for_timeout(300)

        assert page.locator(".face-frame").bounding_box()["width"] != before
        assert not page.console_errors


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
