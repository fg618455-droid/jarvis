"""The stage: the conversation and the gate live under the face.

Talking to the assistant is what the interface is for, so it happens where
the assistant is rather than behind a widget. There is no conversation
destination and no security destination; what was in both is on the stage,
between the face and the thing you type into.

The stage has two states and they are one attribute apart. At rest it is the
face, its name, what it is doing, and one line of the readings that matter,
which is what the page opens as. Once something has been said the exchange
takes the room and the face gives it up. Both are structural, so they are
asserted against a real browser: a layout rule that does not hold is
invisible to an API test and returns 200 all the same.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from jarvis.webui.server import WebUIConfig, WebUIServer


TURN = {
    "turn_id": "t-1",
    "started_at": 1_756_000_000,
    "transcript": "wie spaet ist es",
    "reply": "Es ist kurz nach acht.",
    "total_ms": 1200,
    "source": "text",
    "tools": [],
}

PENDING = {
    "request_id": "r-1",
    "action_name": "openOnComputer",
    "action_args": {"target": "https://example.invalid"},
    "seconds_left": 42.0,
}


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
    opened.on("console", lambda message: (
        opened.console_errors.append(f"{message.type}: {message.text}")
        if message.type == "error" else None
    ))
    opened.on("pageerror", lambda error: opened.console_errors.append(f"pageerror: {error}"))
    yield opened
    context.close()


def _stub(page, *, turns=(), security=None):
    """Answer the two endpoints the stage reads, before any module runs."""
    payload = json.dumps({
        "turns": list(turns),
        "security": security if security is not None else {
            "level": "critical",
            "timeout_seconds": 60,
            "pending": [],
            "decisions": [],
            "channels": [{"name": "web", "available": True}],
        },
    })
    page.add_init_script(
        """(() => {
            const stubbed = %s;
            const real = window.fetch.bind(window);
            window.fetch = (input, init) => {
                const url = String(input?.url || input);
                if (url.includes('/api/conversation')) {
                    return Promise.resolve(new Response(
                        JSON.stringify({turns: stubbed.turns, discarded: {},
                                        conversation_mode: false}),
                        {headers: {'Content-Type': 'application/json'}}));
                }
                if (url.includes('/api/security/decide')) {
                    window.__decided = JSON.parse(init.body);
                    stubbed.security = {...stubbed.security, pending: []};
                    return Promise.resolve(new Response(JSON.stringify({ok: true}),
                        {headers: {'Content-Type': 'application/json'}}));
                }
                if (url.includes('/api/security')) {
                    return Promise.resolve(new Response(JSON.stringify(stubbed.security),
                        {headers: {'Content-Type': 'application/json'}}));
                }
                return real(input, init);
            };
        })()""" % payload
    )


def _open(page, served):
    page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
    page.wait_for_selector(".face-stage[data-stage]", timeout=20000)


class TestTheStageOpensAtRest:
    def test_a_page_with_nothing_said_yet_is_the_face_and_the_dock(self, page, served):
        _stub(page)
        _open(page, served)
        assert page.locator(".face-stage").get_attribute("data-stage") == "resting"
        assert not page.locator(".face-exchange").is_visible()

    def test_the_readings_that_matter_are_on_it(self, page, served):
        """A start screen that shows nothing but a circle makes you open four
        panels to find out whether anything needs you."""
        _stub(page)
        _open(page, served)
        glance = page.locator(".stage-glance")
        assert glance.is_visible()
        assert glance.locator(".stage-reading").count() >= 4

    def test_a_reading_whose_source_never_answered_says_nothing_rather_than_zero(
        self, page, served,
    ):
        """A zero meaning 'no answer' and a zero meaning 'none' are different
        facts, and the gate is where the difference matters."""
        _stub(page)
        page.add_init_script(
            """(() => {
                const real = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const url = String(input?.url || input);
                    if (url.includes('/api/briefing')) return new Promise(() => {});
                    return real(input, init);
                };
            })()"""
        )
        _open(page, served)
        today = page.locator(".stage-reading[data-reading='briefing'] .stage-reading-value")
        page.wait_for_timeout(1200)
        assert today.inner_text().strip() == "—"


class TestSomethingSaidTakesTheRoom:
    def test_a_turn_moves_the_stage_into_the_conversation(self, page, served):
        _stub(page, turns=[TURN])
        _open(page, served)
        page.wait_for_selector(".face-stage[data-stage='talking']", timeout=20000)
        assert page.locator(".exchange-said").first.inner_text() == TURN["transcript"]
        assert page.locator(".exchange-reply").first.inner_text() == TURN["reply"]

    def test_the_glance_gives_way_to_it(self, page, served):
        _stub(page, turns=[TURN])
        _open(page, served)
        page.wait_for_selector(".face-stage[data-stage='talking']", timeout=20000)
        assert not page.locator(".stage-glance").is_visible()

    def test_the_exchange_scrolls_and_the_dock_stays_where_it_is(self, page, served):
        """Two scrollers would mean every gesture had two possible answers and
        the thing you type into would slide out of reach."""
        _stub(page, turns=[{**TURN, "turn_id": f"t-{n}"} for n in range(6)])
        _open(page, served)
        page.wait_for_selector(".face-stage[data-stage='talking']", timeout=20000)
        page.wait_for_timeout(400)

        measured = page.evaluate(
            """() => {
                const stage = document.querySelector('.face-stage');
                const exchange = document.querySelector('.face-exchange');
                const dock = document.querySelector('.face-dock');
                return {
                    stageOverflows: stage.scrollHeight - stage.clientHeight,
                    exchangeScrolls: getComputedStyle(exchange).overflowY,
                    dockBottom: dock.getBoundingClientRect().bottom,
                    stageBottom: stage.getBoundingClientRect().bottom,
                };
            }"""
        )
        assert measured["stageOverflows"] <= 1, "the stage itself scrolled"
        assert measured["exchangeScrolls"] in ("auto", "scroll")
        assert measured["dockBottom"] <= measured["stageBottom"] + 1


class TestTheGateAsksWhereYouAreLooking:
    def test_a_waiting_confirmation_stands_over_the_dock(self, page, served):
        _stub(page, security={
            "level": "critical", "timeout_seconds": 60,
            "pending": [PENDING], "decisions": [],
            "channels": [{"name": "web", "available": True}],
        })
        _open(page, served)
        card = page.locator(".stage-gate .gate-request")
        card.wait_for(timeout=20000)
        assert PENDING["action_name"] in card.inner_text()

        placed = page.evaluate(
            """() => {
                const gate = document.querySelector('.stage-gate');
                const dock = document.querySelector('.face-dock');
                return gate.getBoundingClientRect().bottom
                    <= dock.getBoundingClientRect().top + 1;
            }"""
        )
        assert placed, "the gate did not stand above the dock"

    def test_answering_it_clears_it(self, page, served):
        _stub(page, security={
            "level": "critical", "timeout_seconds": 60,
            "pending": [PENDING], "decisions": [],
            "channels": [{"name": "web", "available": True}],
        })
        _open(page, served)
        page.locator(".stage-gate .gate-request").wait_for(timeout=20000)
        page.locator(".stage-gate .gate-approve").click()
        page.wait_for_selector(".stage-gate .gate-request", state="detached", timeout=20000)
        assert page.evaluate("window.__decided")["approved"] is True

    def test_the_level_in_force_is_readable_without_opening_anything(self, page, served):
        _stub(page, security={
            "level": "off", "timeout_seconds": 60, "pending": [], "decisions": [],
            "channels": [{"name": "web", "available": True}],
        })
        _open(page, served)
        chip = page.locator(".stage-reading[data-reading='security'] .chip").first
        chip.wait_for(timeout=20000)
        assert "off" in chip.inner_text()
        assert "warn" in (chip.get_attribute("class") or ""), (
            "a gate that stops nothing read as healthy"
        )


class TestNeitherIsADestinationAnyMore:
    @pytest.mark.parametrize("gone", ["conversation", "security"])
    def test_its_address_lands_on_the_deck(self, page, served, gone):
        _stub(page)
        page.goto(f"{served}/#/{gone}", wait_until="domcontentloaded")
        page.wait_for_selector(".face-stage[data-stage]", timeout=20000)
        page.wait_for_timeout(400)
        assert page.evaluate("location.hash") == "#/deck"
        assert page.locator(".panel").count() == 0

    @pytest.mark.parametrize("gone", ["conversation", "security"])
    def test_no_widget_offers_it(self, page, served, gone):
        _stub(page)
        _open(page, served)
        assert page.locator(f".widget[data-panel='{gone}']").count() == 0
