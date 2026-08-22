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
    "llm",
    "logs",
    "crew",
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

    def test_every_destination_sits_inside_a_named_navigation_group(self, page, served):
        page.goto(served, wait_until="networkidle")
        page.wait_for_selector(".nav-group", state="visible")

        grouped = page.locator(".nav-group .nav-item").count()

        assert grouped == len(VIEWS), "a destination escaped its group"
        assert page.locator(".nav-item").count() == grouped
        for group in page.locator(".nav-group").all():
            assert group.get_attribute("aria-label"), "a group has no accessible name"

    def test_switching_language_keeps_the_view_you_are_on(self, page, served):
        page.goto(f"{served}/#/tools", wait_until="networkidle")
        page.wait_for_selector("main h1", state="visible")

        page.select_option("header select", "de")
        page.wait_for_timeout(300)

        assert page.evaluate("location.hash") == "#/tools"
        assert not page.console_errors


class TestLlmRouteLayout:
    """The route view keeps every operational detail inside its own card."""

    def _open_with_routes(self, page, served):
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                api.llmRoutes = async () => ({ chains: {
                    fast: [{
                        active: true, blocked_until: null, failures: 1,
                        hits: 4, invalid: false, last_error: '',
                        masked_key: '••••••••LmVX', model: 'meta-llama/llama-prompt-guard-2-22m',
                        name: 'groq', tier: 'fast',
                    }],
                    chat: [{
                        active: false, blocked_until: null, failures: 2,
                        hits: 12, invalid: false, last_error: 'rate limit reached',
                        masked_key: '••••••••kwrd', model: 'zai-glm-4.7',
                        name: 'cerebras', tier: 'chat',
                    }],
                    private: [{
                        active: true, blocked_until: null, failures: 12,
                        hits: 28, invalid: false, last_error: '',
                        masked_key: '', model: 'qwen2.5:7b-ctx8k',
                        name: 'local-private', tier: 'private',
                    }],
                }});
            }"""
        )
        page.goto(f"{served}/#/llm")
        page.wait_for_selector(".llm-chain", state="visible")

    def test_route_details_wrap_within_each_chain_card(self, page, served):
        self._open_with_routes(page, served)

        assert page.locator(".llm-chain").count() == 3
        assert page.get_by_text("meta-llama/llama-prompt-guard-2-22m", exact=True).is_visible()
        assert page.get_by_text("rate limit reached", exact=True).is_visible()

        widths = page.locator(".llm-chain").evaluate_all(
            "cards => cards.map(card => ({ client: card.clientWidth, scroll: card.scrollWidth }))"
        )
        assert all(item["scroll"] <= item["client"] for item in widths)


class TestMemoryMaintenance:
    def _open_with_memory_data(self, page, served):
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                api.graphTree = async () => null;
                api.graphStats = async () => ({ total_nodes: 0, total_tokens: 0 });
                api.graphPresets = async () => ({ ids: [] });
                api.memories = async () => ({ memories: [] });
                api.meals = async () => ({ meals: [{
                    id: 7,
                    ts_utc: '2026-08-09T18:30:00Z',
                    description: '<strong>Tofu bowl</strong>',
                    calories_kcal: 640,
                    protein_g: 31,
                    carbs_g: 72,
                    fat_g: 22,
                }] });
                api.topics = async () => ({ topics: [
                    { name: '<img src=x onerror=alert(1)>', count: 4 },
                    { name: 'local models', count: 2 },
                ] });
                window.__maintenanceCalls = [];
                api.importDiary = async (onEvent) => {
                    window.__maintenanceCalls.push('import-diary');
                    onEvent({ type: 'start', total: 2 });
                    await new Promise(resolve => setTimeout(resolve, 180));
                    onEvent({ type: 'progress', processed: 1, total: 2, date: '2026-08-08', facts: 3 });
                    await new Promise(resolve => setTimeout(resolve, 180));
                    const complete = {
                        type: 'complete', processed: 2, total: 2, total_facts: 5,
                    };
                    onEvent(complete);
                    return complete;
                };
                for (const name of ['consolidateAll', 'scrubDeflections', 'optimiseTopics']) {
                    api[name] = async (onEvent) => {
                        window.__maintenanceCalls.push(name);
                        const complete = { type: 'complete', nodes: 0, rows: 0 };
                        onEvent(complete);
                        return complete;
                    };
                }
            }"""
        )
        page.goto(f"{served}/#/memory")
        page.wait_for_selector("main h1", state="visible")

    def test_memory_view_shows_meals_and_topic_tally_as_text(self, page, served):
        self._open_with_memory_data(page, served)

        assert page.get_by_role("heading", name="Meal log").is_visible()
        assert page.get_by_text("<strong>Tofu bowl</strong>", exact=True).is_visible()
        assert page.get_by_text("640", exact=True).is_visible()
        assert page.get_by_role("heading", name="Topic tally").is_visible()
        assert page.get_by_text("<img src=x onerror=alert(1)>", exact=True).is_visible()
        assert page.locator("main img").count() == 0

    def test_import_diary_shows_running_progress_and_final_summary(self, page, served):
        self._open_with_memory_data(page, served)
        button = page.get_by_role("button", name="Import diary")

        button.click()
        assert button.is_disabled()
        assert page.get_by_text("0 of 2", exact=True).is_visible()
        page.get_by_text("1 of 2", exact=True).wait_for(state="visible")
        page.get_by_text("5 facts imported from 2 diary entries.", exact=True).wait_for(
            state="visible"
        )
        assert not button.is_disabled()

    def test_rewriting_actions_require_confirmation(self, page, served):
        self._open_with_memory_data(page, served)
        dismissed = []

        def dismiss(dialog):
            dismissed.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss)
        for label in ["Consolidate graph", "Clean deflection narration", "Optimise topics"]:
            page.get_by_role("button", name=label).click()

        assert len(dismissed) == 3
        assert "every populated graph node" in dismissed[0]
        assert "stored diary summaries" in dismissed[1]
        assert "topic tags stored with every diary entry" in dismissed[2]
        assert page.evaluate("window.__maintenanceCalls") == []


class TestMissionControl:
    """The crew view against a reading it never has to fetch.

    The test server has no crew endpoint configured, so the first reading is
    stubbed and every later one is pushed the way the daemon's poller pushes
    it. That is the path that matters: the view is fed by the event stream,
    not by a timer of its own.
    """

    ROSTER = ["JARVIS", "DEV", "RESEARCH", "ASSISTANT", "SCHULE", "SCRIBE", "REACH"]

    def _reading(self, entries, agents):
        return {
            "configured": True,
            "reachable": True,
            "checked_at": 1_755_800_000,
            "entries": entries,
            "agents": agents,
            "daily": [
                {"date": f"2026-08-{day:02d}", "count": 0,
                 "success": 0, "partial": 0, "failure": 0}
                for day in range(9, 23)
            ],
        }

    def _agent(self, name, total=0, last_status=None, last_at=None):
        return {
            "name": name, "success": total, "partial": 0, "failure": 0,
            "total": total, "last_at": last_at, "last_status": last_status,
            "daily": [0] * 13 + [total],
        }

    def _open(self, page, served, entries):
        # In roster order, the way the endpoint reports it.
        agents = [
            self._agent("DEV", total=2, last_status="success",
                        last_at="2026-08-22T09:00:00+00:00")
            if name == "DEV"
            else self._agent(name)
            for name in self.ROSTER
        ]
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async (reading) => {
                const { api } = await import('/static/js/api.js');
                api.crew = async () => reading;
                window.__push = async (next) => {
                    const { live } = await import('/static/js/sse.js');
                    live._emit('crew', next);
                };
            }""",
            self._reading(entries, agents),
        )
        page.goto(f"{served}/#/crew")
        page.wait_for_selector(".agent", state="visible")
        return agents

    ENTRIES = [
        {"id": 2, "agent_name": "DEV", "task_description": "Fixed the router",
         "model_used": "gpt-5.6-sol", "status": "success",
         "created_at": "2026-08-22T09:00:00+00:00"},
        {"id": 1, "agent_name": "DEV", "task_description": "Broke the router",
         "model_used": "gpt-5.6-sol", "status": "failure",
         "created_at": "2026-08-22T08:00:00+00:00"},
    ]

    def test_an_agent_with_nothing_logged_is_shown_as_quiet_not_hidden(
        self, page, served,
    ):
        self._open(page, served, self.ENTRIES)

        names = page.locator(".agent-name").all_inner_texts()

        assert names == self.ROSTER, "the roster is not all there"
        reach = page.locator(".agent", has=page.get_by_text("REACH", exact=True))
        assert "quiet" in (reach.get_attribute("class") or "")
        assert not page.console_errors

    def test_the_reading_says_how_old_it_is(self, page, served):
        self._open(page, served, self.ENTRIES)

        assert page.locator(".crew-state").inner_text().strip()
        assert page.locator(".crew-checked").inner_text().strip()

    def test_a_pushed_reading_repaints_without_being_asked_for(self, page, served):
        agents = self._open(page, served, self.ENTRIES)
        assert page.locator(".feed-row").count() == 2

        page.evaluate(
            "reading => window.__push(reading)",
            self._reading([
                {"id": 3, "agent_name": "SCRIBE", "task_description": "Wrote it down",
                 "model_used": "gemini-2.5-flash", "status": "partial",
                 "created_at": "2026-08-22T10:00:00+00:00"},
                *self.ENTRIES,
            ], agents),
        )
        page.wait_for_timeout(300)

        assert page.locator(".feed-row").count() == 3
        assert not page.console_errors

    def test_only_a_line_that_just_arrived_is_marked_as_new(self, page, served):
        agents = self._open(page, served, self.ENTRIES)
        # Nothing is new on the first reading, or everything would be.
        assert page.locator(".feed-row.arrived").count() == 0

        landed = self._reading([
            {"id": 3, "agent_name": "SCRIBE", "task_description": "Wrote it down",
             "model_used": "gemini-2.5-flash", "status": "success",
             "created_at": "2026-08-22T10:00:00+00:00"},
            *self.ENTRIES,
        ], agents)
        page.evaluate("reading => window.__push(reading)", landed)
        page.wait_for_timeout(300)
        assert page.locator(".feed-row.arrived").count() == 1

        # The same line on the next reading is no longer news.
        page.evaluate("reading => window.__push(reading)", landed)
        page.wait_for_timeout(300)
        assert page.locator(".feed-row.arrived").count() == 0

    def test_choosing_an_agent_narrows_the_feed_to_its_work(self, page, served):
        self._open(page, served, self.ENTRIES + [
            {"id": 0, "agent_name": "SCHULE", "task_description": "Read the timetable",
             "model_used": "gpt-5.6-sol", "status": "success",
             "created_at": "2026-08-22T07:00:00+00:00"},
        ])
        assert page.locator(".feed-row").count() == 3

        page.locator(".agent", has=page.get_by_text("DEV", exact=True)).click()
        page.wait_for_timeout(200)

        assert set(page.locator(".feed-agent").all_inner_texts()) == {"DEV"}
        assert page.locator(".feed-row").count() == 2
        assert not page.console_errors

    def test_a_failing_filter_says_so_rather_than_showing_an_empty_feed(
        self, page, served,
    ):
        self._open(page, served, [self.ENTRIES[0]])

        page.get_by_role("button", name="Failures only").click()
        page.wait_for_timeout(200)

        assert page.locator(".feed-row").count() == 0
        assert page.get_by_text("Nothing here matches the filter.").is_visible()

    def test_what_an_agent_logged_is_rendered_as_text(self, page, served):
        """The task text comes from a machine this daemon does not own."""
        self._open(page, served, [{
            "id": 9, "agent_name": "<img src=x onerror=alert(1)>",
            "task_description": "<script>alert(1)</script> shipped",
            "model_used": "gpt-5.6-sol", "status": "success",
            "created_at": "2026-08-22T09:00:00+00:00",
        }])

        assert page.get_by_text("<script>alert(1)</script> shipped", exact=True).is_visible()
        assert page.locator("main img").count() == 0
        assert page.locator("main script").count() == 0


class TestStreamingApiClient:
    @pytest.mark.parametrize(
        ("method_name", "path"),
        [
            ("importDiary", "/api/graph/import-diary"),
            ("consolidateAll", "/api/graph/consolidate-all"),
            ("scrubDeflections", "/api/diary/scrub-deflections"),
            ("optimiseTopics", "/api/diary/optimise-topics"),
        ],
    )
    def test_maintenance_clients_post_to_their_streaming_endpoint(
        self, page, served, method_name, path
    ):
        seen = []

        def respond(route, request):
            seen.append((request.method, request.headers.get("x-jarvis-ui")))
            route.fulfill(
                status=200,
                content_type="application/x-ndjson",
                body='{"type":"complete"}\n',
            )

        page.route(f"**{path}", respond)
        page.goto(served, wait_until="networkidle")
        result = page.evaluate(
            """async (methodName) => {
                const { api } = await import('/static/js/api.js');
                return api[methodName](() => {});
            }""",
            method_name,
        )

        assert result == {"type": "complete"}
        assert seen == [("POST", "1")]

    def test_import_diary_reads_every_ndjson_event(self, page, served):
        seen_request = {}

        def respond(route, request):
            seen_request["method"] = request.method
            seen_request["ui_header"] = request.headers.get("x-jarvis-ui")
            route.fulfill(
                status=200,
                content_type="application/x-ndjson",
                body=(
                    '{"type":"start","total":2}\n'
                    '{"type":"progress","processed":1,"total":2,"facts":3}\n'
                    '{"type":"complete","processed":2,"total":2,"total_facts":5}\n'
                ),
            )

        page.route("**/api/graph/import-diary", respond)
        page.goto(served, wait_until="networkidle")
        result = page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                const events = [];
                const complete = await api.importDiary(event => events.push(event));
                return { events, complete };
            }"""
        )

        assert [event["type"] for event in result["events"]] == [
            "start", "progress", "complete",
        ]
        assert result["complete"]["total_facts"] == 5
        assert seen_request == {"method": "POST", "ui_header": "1"}
