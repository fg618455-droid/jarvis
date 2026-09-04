"""What the header says the assistant is doing, in the reader's terms.

The runtime phase is one word, and the same word covers two situations a
reader would never call the same thing. `idle` while a conversation runs
does not mean "waiting for the wake word": nothing needs the wake word
then. `capturing` outside a conversation does not mean "listening to you":
the microphone is open to the whole room, and what it hears is checked for
the wake word, or written down for the passive record, and usually neither
is addressed to Jarvis.

A phase read out in words that no longer fit the situation is how a user
comes to believe the assistant ignored them, so the words are asserted
here rather than left to the phase table.
"""

from __future__ import annotations

import socket
import threading

import pytest

from jarvis.runtime import get_runtime_state
from jarvis.runtime.state import Phase
from jarvis.webui.server import WebUIConfig, WebUIMode, WebUIServer


LANGUAGES = ["en", "de"]

# Everything `idle` and `capturing` can actually mean, as the page sees it.
READINGS = {
    "idle-alone": ("idle", {"conversation": False, "passive": False}),
    "idle-in-conversation": ("idle", {"conversation": True, "passive": False}),
    "capturing-in-conversation": ("capturing", {"conversation": True, "passive": False}),
    "capturing-for-the-record": ("capturing", {"conversation": False, "passive": True}),
    "capturing-for-the-wake-word": ("capturing", {"conversation": False, "passive": False}),
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
            launched = driver.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure is a skip
            pytest.skip(f"chromium is not available: {exc}")
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def served() -> str:
    """A control centre with nothing behind it, for reading the words."""
    cfg = WebUIConfig(
        host="127.0.0.1", port=_free_port(), token="", mode=WebUIMode.STANDALONE,
    )
    server = WebUIServer(cfg)
    server.start()
    threading.Event().wait(0.5)
    yield cfg.url
    server.stop()


@pytest.fixture(scope="module")
def attached() -> str:
    """A control centre reading the live runtime, as it does beside a daemon."""
    cfg = WebUIConfig(
        host="127.0.0.1",
        port=_free_port(),
        token="",
        mode=WebUIMode.DAEMON_ATTACHED,
    )
    server = WebUIServer(cfg)
    server.start()
    threading.Event().wait(0.5)
    yield cfg.url
    server.stop()
    get_runtime_state().reset()


def _labels(page, served: str, language: str) -> dict[str, str]:
    """Every reading said in words, in one language."""
    return page.evaluate(
        """async ([base, language, readings]) => {
            const i18n = await import(base + '/static/js/i18n.js');
            const { phaseLabel } = await import(base + '/static/js/phase.js');
            i18n.setLanguage(language);
            const said = {};
            for (const [name, [phase, reading]] of Object.entries(readings)) {
                said[name] = phaseLabel(phase, reading);
            }
            return said;
        }""",
        [served, language, {k: list(v) for k, v in READINGS.items()}],
    )


def _wake_word_words(page, served: str, language: str) -> str:
    return page.evaluate(
        """async ([base, language]) => {
            const i18n = await import(base + '/static/js/i18n.js');
            i18n.setLanguage(language);
            return i18n.t('phase.idle');
        }""",
        [served, language],
    )


@pytest.fixture
def page(browser, served):
    context = browser.new_context()
    opened = context.new_page()
    opened.goto(f"{served}/#/deck", wait_until="domcontentloaded")
    yield opened
    context.close()


@pytest.fixture
def live_page(browser):
    context = browser.new_context()
    opened = context.new_page()
    yield opened
    context.close()


class TestThePhaseIsSaidAsItIs:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_a_running_conversation_never_asks_for_the_wake_word(
        self, page, served, language
    ):
        """No question needs the name while a conversation is open."""
        said = _labels(page, served, language)
        wake_word = _wake_word_words(page, served, language)

        assert said["idle-in-conversation"] != wake_word
        assert said["capturing-in-conversation"] != wake_word

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_an_unaddressed_room_is_not_reported_as_listening_to_you(
        self, page, served, language
    ):
        """Voice detected is not the same as being spoken to."""
        said = _labels(page, served, language)

        assert said["capturing-for-the-record"] != said["capturing-in-conversation"]
        assert said["capturing-for-the-wake-word"] != said["capturing-in-conversation"]

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_situation_reads_differently(self, page, served, language):
        """Five situations, five sentences: a shared one hides a difference."""
        said = _labels(page, served, language)

        assert len(set(said.values())) == len(READINGS), said

    def test_a_dropped_connection_outranks_the_phase(self, page, served):
        """A stale reading is worse than saying the page is on its own."""
        label = page.evaluate(
            """async (base) => {
                const i18n = await import(base + '/static/js/i18n.js');
                const { phaseLabel } = await import(base + '/static/js/phase.js');
                return [
                    phaseLabel('thinking', { connected: false }),
                    i18n.t('common.reconnecting'),
                ];
            }""",
            served,
        )

        assert label[0] == label[1]

    def test_a_phase_the_page_does_not_know_reads_as_not_running(self, page, served):
        label = page.evaluate(
            """async (base) => {
                const i18n = await import(base + '/static/js/i18n.js');
                const { phaseLabel } = await import(base + '/static/js/phase.js');
                return [phaseLabel('bananas', {}), i18n.t('phase.offline')];
            }""",
            served,
        )

        assert label[0] == label[1]


class TestTheHeaderReadsTheSituation:
    """What the page shows, driven by the daemon's own live state.

    The helper could be right on its own and the header still wrong, so the
    runtime is moved through the situations a user is actually in, and the
    header is read back over the stream that carries them.
    """

    def _header(
        self, page, attached: str, phase: Phase, *, conversation: bool, passive: bool
    ) -> str:
        state = get_runtime_state()
        state.set_conversation_active(conversation)
        state.set_passive_enabled(passive)
        state.set_phase(phase)
        page.goto(f"{attached}/#/deck", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.getElementById('phase-text').textContent.trim() !== ''",
            timeout=8000,
        )
        return page.locator("#phase-text").inner_text()

    def test_it_does_not_ask_for_the_wake_word_during_a_conversation(
        self, live_page, attached
    ):
        in_conversation = self._header(
            live_page, attached, Phase.IDLE, conversation=True, passive=False
        )
        alone = self._header(
            live_page, attached, Phase.IDLE, conversation=False, passive=False
        )

        assert in_conversation != alone

    def test_it_says_the_room_is_written_down_rather_than_listened_to(
        self, live_page, attached
    ):
        for_the_record = self._header(
            live_page, attached, Phase.CAPTURING, conversation=False, passive=True
        )
        spoken_to = self._header(
            live_page, attached, Phase.CAPTURING, conversation=True, passive=False
        )

        assert for_the_record != spoken_to

    def test_the_header_follows_a_conversation_started_while_it_watches(
        self, live_page, attached
    ):
        """A conversation can start without this page asking for it."""
        before = self._header(
            live_page, attached, Phase.IDLE, conversation=False, passive=False
        )

        get_runtime_state().set_conversation_active(True)

        live_page.wait_for_function(
            "(before) => document.getElementById('phase-text').textContent.trim() !== before",
            arg=before,
            timeout=8000,
        )
        assert live_page.locator("#phase-text").inner_text() != before
