"""Leaving an editor that is holding changes nobody has saved.

Three places in this interface hold typed changes and write them in one go:
Settings, the MCP editor and the LLM route editor. The MCP editor is the one
that decides the rule. What is typed into it is credentials, which are read
back masked, so a change discarded there is not a change to make again, it is
a secret to go and find again.

Leaving is one thing with several doors: the close button, Escape, the
browser's back button, the widget for another panel, the way out of Settings.
They all end at the same address change, so the ask lives there.

The other half of the rule is that it stays quiet. A warning on every panel
switch would be trained away inside a day, and then the one that mattered
would be clicked through as fast as the rest.
"""

from __future__ import annotations

import socket
import threading

import pytest

from jarvis.webui.server import WebUIConfig, WebUIServer


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
    context = browser.new_context()
    opened = context.new_page()
    opened.console_errors = []
    opened.asked = []
    opened.on("console", lambda message: (
        opened.console_errors.append(f"{message.type}: {message.text}")
        if message.type in ("error", "warning") else None
    ))
    opened.on("pageerror", lambda error: opened.console_errors.append(f"pageerror: {error}"))
    yield opened
    context.close()


def _answer(page, *, leave: bool):
    """Answer whatever the page asks, and remember that it asked."""
    def answered(dialog):
        page.asked.append(dialog.message)
        dialog.accept() if leave else dialog.dismiss()

    page.on("dialog", answered)


def _open_panel(page, served, panel):
    page.goto(f"{served}/#/{panel}")
    page.wait_for_selector('.panel[aria-busy="false"]', timeout=20000)


def _open_settings(page, served):
    page.goto(f"{served}/#/settings")
    page.wait_for_selector(".view-settings .settings-nav", state="visible", timeout=20000)


ONE_SERVER = """async () => {
    const { api } = await import('/static/js/api.js');
    api.mcpServers = async () => ({ servers: [{
        _index: 0, name: 'files', command: 'uvx', args: ['mcp-server-files'],
        env: { API_KEY: '\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022' },
        timeout_sec: null, idle_timeout_sec: null,
        tool_count: 3, connected: true,
    }] });
}"""


class TestTheMcpEditorIsAskedBeforeItIsLeft:
    """The editor whose fields are credentials read back masked."""

    def _typed_into(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.evaluate(ONE_SERVER)
        _open_panel(page, served, "mcp")
        field = page.get_by_label("Command", exact=True).first
        field.fill("uvx --from somewhere-else")
        return field

    def test_leaving_with_a_typed_change_asks_first(self, page, served):
        self._typed_into(page, served)
        _answer(page, leave=False)

        page.locator(".panel-close").click()
        page.wait_for_timeout(500)

        assert page.asked, "the editor was left without a word"
        assert page.locator(".panel").count() == 1, "it left anyway"
        assert page.evaluate("location.hash") == "#/mcp"

    def test_what_was_typed_is_still_there_after_refusing(self, page, served):
        self._typed_into(page, served)
        _answer(page, leave=False)

        page.locator(".panel-close").click()
        page.wait_for_timeout(500)

        assert page.get_by_label("Command", exact=True).first.input_value() == (
            "uvx --from somewhere-else"
        ), "the change was discarded by the departure that was refused"

    def test_saying_yes_leaves(self, page, served):
        self._typed_into(page, served)
        _answer(page, leave=True)

        page.locator(".panel-close").click()
        page.wait_for_selector(".panel", state="detached", timeout=20000)

        assert page.asked
        assert page.evaluate("location.hash") == "#/deck"

    def test_escape_outside_a_field_asks_too(self, page, served):
        """Escape is the reflex the ask exists for."""
        self._typed_into(page, served)
        _answer(page, leave=False)
        page.locator(".panel-title").click()

        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        assert page.asked
        assert page.locator(".panel").count() == 1

    def test_escape_inside_a_field_is_still_left_to_the_field(self, page, served):
        """The rule already in place, which this one must not contradict.

        In a field, Escape belongs to the field: it never reached the panel
        before and it must not start reaching it now, or the ask would be
        raised for a key press that was never a departure.
        """
        field = self._typed_into(page, served)
        _answer(page, leave=False)
        field.click()

        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        assert not page.asked, "a key press inside a field was treated as leaving"
        assert page.locator(".panel").count() == 1

    def test_the_browser_back_button_asks(self, page, served):
        self._typed_into(page, served)
        _answer(page, leave=False)

        page.go_back()
        page.wait_for_timeout(600)

        assert page.asked
        assert page.evaluate("location.hash") == "#/mcp"

    def test_another_panel_asks(self, page, served):
        """Opening something else is leaving this."""
        self._typed_into(page, served)
        _answer(page, leave=False)

        # A widget in the left rail: the right one is behind the open panel.
        page.locator('.widget[data-panel="memory"] .widget-open').click()
        page.wait_for_timeout(600)

        assert page.asked
        assert page.evaluate("location.hash") == "#/mcp"


class TestQuietWhenThereIsNothingToLose:
    """A warning nobody has earned is a warning nobody reads."""

    def test_opening_and_closing_an_editor_asks_nothing(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.evaluate(ONE_SERVER)
        _open_panel(page, served, "mcp")
        _answer(page, leave=False)

        page.locator(".panel-close").click()
        page.wait_for_selector(".panel", state="detached", timeout=20000)

        assert not page.asked, f"asked with nothing typed: {page.asked}"
        assert page.evaluate("location.hash") == "#/deck"

    def test_walking_through_every_panel_asks_nothing(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        _answer(page, leave=False)

        for panel in ["mcp", "llm-routes", "tools", "logs", "mcp", "deck"]:
            if panel == "deck":
                page.goto(f"{served}/#/deck")
                page.wait_for_selector(".panel", state="detached", timeout=20000)
            else:
                _open_panel(page, served, panel)

        assert not page.asked, f"asked on a plain panel switch: {page.asked}"

    def test_a_change_that_was_typed_back_out_again_asks_nothing(self, page, served):
        """Settings compares with what is stored rather than counting keys."""
        _open_settings(page, served)
        field = page.locator(".view-settings .field input[type='text']").first
        was = field.input_value()
        field.fill(f"{was}x")
        field.fill(was)
        _answer(page, leave=False)

        page.locator(".settings-back").click()
        page.wait_for_timeout(600)

        assert not page.asked, "a field put back the way it was counted as a change"
        assert page.evaluate("location.hash") == "#/deck"

    def test_a_saved_editor_asks_nothing(self, page, served):
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.evaluate(ONE_SERVER)
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                api.saveMcpServers = async () => ({ servers: [] });
            }"""
        )
        _open_panel(page, served, "mcp")
        page.get_by_label("Command", exact=True).first.fill("uvx --from elsewhere")
        page.get_by_role("button", name="Save").first.click()
        page.wait_for_selector(".toast", timeout=20000)
        _answer(page, leave=False)

        page.locator(".panel-close").click()
        page.wait_for_selector(".panel", state="detached", timeout=20000)

        assert not page.asked, "asked about changes that had just been written"


class TestSettingsAndTheRouteEditorAreAskedTheSameWay:
    def test_settings_asks_before_its_way_out(self, page, served):
        _open_settings(page, served)
        field = page.locator(".view-settings .field input[type='text']").first
        field.fill(f"{field.input_value()}-changed")
        _answer(page, leave=False)

        page.locator(".settings-back").click()
        page.wait_for_timeout(600)

        assert page.asked
        assert page.evaluate("location.hash") == "#/settings"
        assert page.locator(".view-settings").count() == 1

    def test_the_route_editor_asks_before_it_is_left(self, page, served):
        _open_panel(page, served, "llm-routes")
        page.get_by_role("button", name="Add route").click()
        page.wait_for_timeout(200)
        _answer(page, leave=False)

        page.locator(".panel-close").click()
        page.wait_for_timeout(600)

        assert page.asked
        assert page.locator(".panel").count() == 1
        assert page.evaluate("location.hash") == "#/llm-routes"

    def test_reloading_the_route_editor_in_place_asks_too(self, page, served):
        """Reloading is discarding, whichever button asked for it.

        Probing the models and resetting the cooldowns both replace the
        editor's copy with what is stored, which throws away anything typed
        into it just as surely as leaving the view does.
        """
        _open_panel(page, served, "llm-routes")
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                window.__reset = 0;
                api.resetLlmRoutes = async () => { window.__reset += 1; return {}; };
            }"""
        )
        page.get_by_role("button", name="Add route").click()
        before = page.locator(".route-config").count()
        _answer(page, leave=False)

        page.get_by_role("button", name="Reset cooldowns").click()
        page.wait_for_timeout(600)

        assert page.asked, "the editor was reloaded over what was typed into it"
        assert page.evaluate("window.__reset") == 0, "it reset anyway"
        assert page.locator(".route-config").count() == before, (
            "the route that was added is gone"
        )

    def test_the_route_editor_is_quiet_until_something_is_changed(self, page, served):
        _open_panel(page, served, "llm-routes")
        _answer(page, leave=False)

        page.locator(".panel-close").click()
        page.wait_for_selector(".panel", state="detached", timeout=20000)

        assert not page.asked, f"asked with nothing changed: {page.asked}"
