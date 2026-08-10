"""The control centre rendered in a real browser.

The API tests prove each endpoint answers; they cannot prove the page that
consumes it renders. A view that reads a field the API does not send fails
silently in JavaScript and returns 200 all the same, so the only honest
check is to load the thing and click through it.

Two properties are asserted for every view: nothing lands in the console,
and nothing is fetched from outside the server's own origin. The second is
the offline rule, which a stray font or CDN reference would break without
any visible symptom.
"""

from __future__ import annotations

import socket
import threading

import pytest

from jarvis.webui.server import WebUIConfig, WebUIServer


VIEWS = [
    "overview",
    "memory",
    "conversation",
    "tools",
    "security",
    "system",
    "settings",
]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    """A headless Chromium, skipped when the browser is not installed."""
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
    """The real threaded server, on a port nothing else is using."""
    cfg = WebUIConfig(host="127.0.0.1", port=_free_port(), token="")
    server = WebUIServer(cfg)
    server.start()
    ready = threading.Event()
    ready.wait(0.5)
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


class TestEveryViewRenders:
    def test_each_view_paints_its_heading_without_console_errors(self, page, served):
        page.goto(served, wait_until="networkidle")

        for view in VIEWS:
            page.goto(f"{served}/#/{view}")
            page.wait_for_selector("main h1", state="visible", timeout=5000)
            page.wait_for_timeout(400)
            assert page.locator("main h1").inner_text().strip(), f"{view} has no heading"
            assert not page.console_errors, f"{view}: {page.console_errors}"

    def test_nothing_is_fetched_from_outside_the_server(self, page, served):
        page.goto(served, wait_until="networkidle")

        for view in VIEWS:
            page.goto(f"{served}/#/{view}")
            page.wait_for_timeout(400)

        assert not page.foreign_requests, f"outbound: {page.foreign_requests}"

    def test_switching_language_keeps_the_view_you_are_on(self, page, served):
        page.goto(f"{served}/#/tools", wait_until="networkidle")
        page.wait_for_selector("main h1", state="visible")

        page.select_option("header select", "de")
        page.wait_for_timeout(300)

        assert page.evaluate("location.hash") == "#/tools"
        assert not page.console_errors
