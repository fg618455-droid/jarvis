"""The face: what it draws, and what it still says when it cannot move.

The face is the largest painted object in the interface and the one thing on
the deck that is not text, so what it is doing has to survive two readers who
see it very differently: one watching it move, and one who has asked for no
motion at all and is looking at a still picture.

That is the property worth asserting. Not "does it animate", which is a
matter of taste, but "with every animation switched off, are the four states
it reports still four different pictures". A face that separated `listening`
from `thinking` only by the speed of a rotation would pass every rendering
test there is and tell a reader with `prefers-reduced-motion` nothing.

It is also painted from `var(--accent)` rather than from a colour of its own,
so a theme drives it for free and there is no palette to keep in step.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from jarvis.webui.server import WebUIConfig, WebUIMode, WebUIServer


STATES = ["idle", "listening", "thinking", "speaking"]


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


def _reading(state: str) -> dict:
    """What `/api/visualizer/state` says, shaped exactly as `state.py` writes it."""
    speaking = state == "speaking"
    return {
        "state": state,
        "level": 0.6 if speaking else 0.0,
        # Raw playback samples rather than a normalised curve, because that is
        # what a TTS engine actually feeds in.
        "samples": [
            (900 * (1 if index % 3 else -1)) if speaking else 0.0
            for index in range(64)
        ],
        "alert": False,
        "loading": False,
    }


def _face_in(page, served, state, *, reduced=False):
    """The canvas as pixels, with the daemon pinned to one reported state."""
    page.route(
        "**/api/visualizer/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_reading(state)),
        ),
    )
    page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
    page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
    # Long enough for a poll to land and, where motion is allowed, for the
    # drawing to have settled into that state.
    page.wait_for_timeout(900)
    return page.evaluate(
        """() => {
            const canvas = document.querySelector('.face-canvas');
            return canvas.toDataURL('image/png');
        }"""
    )


class TestTheFaceIsDrawnHere:
    def test_nothing_is_framed(self, browser, served):
        """A first-party face is in the page, not in a window onto someone else's."""
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)

            assert page.locator(".face-stage iframe").count() == 0
        finally:
            context.close()

    def test_it_paints_in_the_theme_accent(self, browser, served):
        """The face reads the accent rather than holding a colour of its own."""
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
            page.wait_for_timeout(700)

            painted = page.evaluate(
                """() => {
                    const canvas = document.querySelector('.face-canvas');
                    const ctx = canvas.getContext('2d');
                    const { data, width, height } = ctx.getImageData(
                        0, 0, canvas.width, canvas.height
                    );
                    // The middle of the drawing is the disc, whatever its size.
                    const at = ((height >> 1) * width + (width >> 1)) * 4;
                    return [data[at], data[at + 1], data[at + 2], data[at + 3]];
                }"""
            )
            accent = page.evaluate(
                """() => {
                    const probe = document.createElement('canvas');
                    probe.width = probe.height = 1;
                    const ctx = probe.getContext('2d');
                    ctx.fillStyle = getComputedStyle(document.documentElement)
                        .getPropertyValue('--accent').trim();
                    ctx.fillRect(0, 0, 1, 1);
                    return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
                }"""
            )

            assert painted[3] > 200, "the centre of the face is not painted at all"
            for channel, (drawn, wanted) in enumerate(zip(painted, accent)):
                assert abs(drawn - wanted) <= 12, (
                    f"channel {channel}: face paints {painted[:3]}, "
                    f"accent is {accent}"
                )
        finally:
            context.close()


class TestEveryStateSurvivesMotionBeingOff:
    """The reason this face was chosen over a disc or a ring.

    With `prefers-reduced-motion` honoured, nothing moves, so anything the
    face was carrying in movement alone is simply gone. What is left has to
    still be four different pictures.
    """

    def test_the_four_states_are_four_different_pictures(self, browser, served):
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()
        try:
            drawn = {state: _face_in(page, served, state, reduced=True) for state in STATES}
        finally:
            context.close()

        collisions = [
            (a, b)
            for index, a in enumerate(STATES)
            for b in STATES[index + 1:]
            if drawn[a] == drawn[b]
        ]
        assert not collisions, (
            f"with motion off these states draw the same picture: {collisions}"
        )

    def test_nothing_moves_when_motion_is_refused(self, browser, served):
        """A canvas is painted from JavaScript, which no stylesheet can stop."""
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()
        try:
            page.route(
                "**/api/visualizer/state",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_reading("thinking")),
                ),
            )
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
            page.wait_for_timeout(700)

            grab = """() => document.querySelector('.face-canvas').toDataURL('image/png')"""
            first = page.evaluate(grab)
            page.wait_for_timeout(600)
            second = page.evaluate(grab)

            assert first == second, "the face is still animating with motion refused"
        finally:
            context.close()


class TestTheFaceIsSizedFromThisInterface:
    def test_size_is_this_browsers_preference(self, browser, served):
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
            page.locator(".face-settings-open").click()
            page.wait_for_selector(".face-settings", state="visible")
            before = page.locator(".face-canvas").bounding_box()["width"]

            page.fill(".face-settings [name='size']", "460")
            page.dispatch_event(".face-settings [name='size']", "input")
            page.wait_for_timeout(300)

            assert page.locator(".face-canvas").bounding_box()["width"] != before
        finally:
            context.close()

    def test_the_size_survives_a_reload(self, browser, served):
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
            page.locator(".face-settings-open").click()
            page.wait_for_selector(".face-settings", state="visible")
            page.fill(".face-settings [name='size']", "240")
            page.dispatch_event(".face-settings [name='size']", "input")
            page.wait_for_timeout(300)

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
            page.wait_for_timeout(300)

            assert page.locator(".face-canvas").bounding_box()["width"] == pytest.approx(
                240, abs=2
            )
        finally:
            context.close()

    def test_there_is_no_gallery_to_pick_from(self, browser, served):
        """One face, ours. A picker would be choosing between vendored pages."""
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
            page.locator(".face-settings-open").click()
            page.wait_for_selector(".face-settings", state="visible")

            assert page.locator(".face-settings [name='face']").count() == 0
        finally:
            context.close()
