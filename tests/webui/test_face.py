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

# The face is drawn from the centre outwards, so every measurement below is a
# radius as a share of the canvas's width. The casing ring sits at four tenths
# of the width from the centre, and the core at rest is barely a fifth of it:
# the band between them is the reactor's own, and it is what tells a machine
# whether the face is a lit instrument or a filled circle.
COIL_BAND = (0.32, 0.38)

# The most common fully opaque colour in the drawing. The core is by far the
# largest solid thing on the canvas, so this is the colour the face reads as
# from across a room, and asking for it is steadier than sampling one pixel:
# where exactly a hot centre gives way to the body of the core is the
# drawing's business, not the test's.
DOMINANT = """() => {
    const canvas = document.querySelector('.face-canvas');
    const { data } = canvas.getContext('2d')
        .getImageData(0, 0, canvas.width, canvas.height);
    const seen = new Map();
    for (let at = 0; at < data.length; at += 4) {
        if (data[at + 3] < 250) continue;
        const key = (data[at] << 16) | (data[at + 1] << 8) | data[at + 2];
        seen.set(key, (seen.get(key) || 0) + 1);
    }
    let best = 0;
    let most = -1;
    for (const [key, count] of seen) {
        if (count > most) { most = count; best = key; }
    }
    return [(best >> 16) & 255, (best >> 8) & 255, best & 255];
}"""

ACCENT = """() => {
    const probe = document.createElement('canvas');
    probe.width = probe.height = 1;
    const ctx = probe.getContext('2d');
    ctx.fillStyle = getComputedStyle(document.documentElement)
        .getPropertyValue('--accent').trim();
    ctx.fillRect(0, 0, 1, 1);
    return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
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

            painted = page.evaluate(DOMINANT)
            accent = page.evaluate(ACCENT)

            for channel, (drawn, wanted) in enumerate(zip(painted, accent)):
                assert abs(drawn - wanted) <= 12, (
                    f"channel {channel}: face paints {painted}, accent is {accent}"
                )
        finally:
            context.close()


class TestTheFaceFollowsTheTheme:
    """Changing the palette repaints the face, motion or no motion.

    With motion refused there is no animation loop, so nothing redraws the
    canvas on its own and the face keeps the accent it was first painted in.
    The page around it changes colour and the largest object on it does not.
    """

    @pytest.mark.parametrize("reduced", [False, True], ids=["motion", "no-motion"])
    def test_switching_theme_repaints_the_face(self, browser, served, reduced):
        context = browser.new_context(
            viewport={"width": 1600, "height": 950},
            reduced_motion="reduce" if reduced else "no-preference",
        )
        page = context.new_page()
        try:
            page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
            page.wait_for_selector(".face-canvas", state="visible", timeout=8000)
            page.wait_for_timeout(900)
            before = page.evaluate(DOMINANT)

            page.evaluate("() => { document.documentElement.dataset.theme = 'ember'; }")
            page.wait_for_timeout(900)

            after = page.evaluate(DOMINANT)
            wanted = page.evaluate(ACCENT)

            assert after != before, "the face kept the palette it was painted in"
            for drawn, target in zip(after, wanted):
                assert abs(drawn - target) <= 12, (
                    f"the face paints {after} where the accent is {wanted}"
                )
        finally:
            context.close()


class TestTheFaceIsAReactorRatherThanADisc:
    """A lit instrument, not a coloured circle.

    The face is the one thing on the deck that is not text, and it is the
    picture of an assistant that is running: it has a hot core, a casing, and
    machinery between the two that keeps turning while nothing is being asked
    of it. A flat disc says the page is painted; a reactor says the daemon is
    alive, which is the same fact the phase pill beside it reports in words.
    """

    COIL_COVERAGE = """(band) => {
        const canvas = document.querySelector('.face-canvas');
        const { data, width, height } = canvas.getContext('2d')
            .getImageData(0, 0, canvas.width, canvas.height);
        const cx = width / 2;
        const cy = height / 2;
        let lit = 0;
        let seen = 0;
        for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
                const radius = Math.hypot(x - cx, y - cy) / width;
                if (radius < band[0] || radius > band[1]) continue;
                seen += 1;
                if (data[(y * width + x) * 4 + 3] >= 90) lit += 1;
            }
        }
        return seen ? lit / seen : 0;
    }"""

    # The band's brightness sector by sector. Turning machinery moves light
    # between sectors; an aura painted once does not.
    COIL_SECTORS = """(band) => {
        const canvas = document.querySelector('.face-canvas');
        const { data, width, height } = canvas.getContext('2d')
            .getImageData(0, 0, canvas.width, canvas.height);
        const cx = width / 2;
        const cy = height / 2;
        const sectors = new Array(24).fill(0);
        for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
                const radius = Math.hypot(x - cx, y - cy) / width;
                if (radius < band[0] || radius > band[1]) continue;
                const angle = Math.atan2(y - cy, x - cx) + Math.PI;
                const sector = Math.min(23, Math.floor((angle / (Math.PI * 2)) * 24));
                sectors[sector] += data[(y * width + x) * 4 + 3];
            }
        }
        return sectors;
    }"""

    # The centre against the body of the core. The body is sampled well inside
    # the smallest core the face ever draws and well outside its hot centre,
    # so the comparison holds in every state.
    CORE_AGAINST_BODY = """() => {
        const canvas = document.querySelector('.face-canvas');
        const { data, width, height } = canvas.getContext('2d')
            .getImageData(0, 0, canvas.width, canvas.height);
        const cx = Math.round(width / 2);
        const cy = Math.round(height / 2);
        const luma = (x, y) => {
            const at = (y * width + x) * 4;
            return 0.2126 * data[at] + 0.7152 * data[at + 1] + 0.0722 * data[at + 2];
        };
        let body = 0;
        const around = 64;
        for (let step = 0; step < around; step += 1) {
            const angle = (step / around) * Math.PI * 2;
            body += luma(
                Math.round(cx + Math.cos(angle) * width * 0.16),
                Math.round(cy + Math.sin(angle) * width * 0.16),
            );
        }
        return { core: luma(cx, cy), body: body / around };
    }"""

    def _pinned(self, page, served, state):
        page.route(
            "**/api/visualizer/state",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_reading(state)),
            ),
        )
        page.goto(f"{served}/#/deck", wait_until="domcontentloaded")
        page.wait_for_selector(".face-canvas", state="visible", timeout=5000)
        page.wait_for_timeout(900)

    @pytest.mark.parametrize("state", STATES)
    def test_there_is_machinery_between_the_core_and_the_rim(
        self, browser, served, state
    ):
        """The band inside the casing ring carries the reactor's coils."""
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()
        try:
            self._pinned(page, served, state)
            coverage = page.evaluate(self.COIL_COVERAGE, list(COIL_BAND))
        finally:
            context.close()

        assert coverage >= 0.25, (
            f"in {state} only {coverage:.0%} of the band between the core and "
            "the casing is painted: the face is a circle in an empty ring"
        )

    def test_the_core_is_hotter_than_its_own_body(self, browser, served):
        """A reactor is brightest where the power is, not evenly coloured."""
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()
        try:
            self._pinned(page, served, "idle")
            read = page.evaluate(self.CORE_AGAINST_BODY)
        finally:
            context.close()

        assert read["core"] >= read["body"] + 25, (
            f"the centre reads {read['core']:.0f} against a body of "
            f"{read['body']:.0f}: the core is flat"
        )

    def test_it_keeps_turning_while_it_idles(self, browser, served):
        """Idle is running, not parked.

        Measured outside the core on purpose. A core that only breathes moves
        nothing but its own edge, and a face whose casing is dead reads as a
        screenshot of an assistant between two words of a sentence.
        """
        context = browser.new_context()
        page = context.new_page()
        try:
            self._pinned(page, served, "idle")
            first = page.evaluate(self.COIL_SECTORS, list(COIL_BAND))
            page.wait_for_timeout(700)
            second = page.evaluate(self.COIL_SECTORS, list(COIL_BAND))
        finally:
            context.close()

        moved = [abs(a - b) / max(1.0, (a + b) / 2) for a, b in zip(first, second)]
        assert max(moved) >= 0.05, (
            "nothing in the casing moved over two thirds of a second while the "
            f"face idled: {[round(share, 3) for share in moved]}"
        )


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
            # Long enough for the first reading to have landed and repainted
            # the face. Grabbing before it arrives would catch the repaint it
            # causes and read it as the face animating.
            page.wait_for_timeout(900)

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

            # Downwards, so the assertion holds in a window too narrow to
            # grant a larger face: the cap can refuse to grow it and can never
            # refuse to shrink it.
            page.fill(".face-settings [name='size']", "200")
            page.dispatch_event(".face-settings [name='size']", "input")
            page.wait_for_timeout(300)

            after = page.locator(".face-canvas").bounding_box()["width"]
            assert after < before, f"the face stayed at {before}px"
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
