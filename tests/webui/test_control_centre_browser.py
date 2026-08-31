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

from jarvis.webui.server import WebUIConfig, WebUIMode, WebUIServer


VIEWS = [
    "overview",
    "memory",
    "conversation",
    "visualizer",
    "passive",
    "tools",
    "security",
    "system",
    "settings",
    "llm-routes",
    "logs",
    "crew",
]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    """A headless Chromium, skipped when the browser is not installed.

    It is given a synthetic microphone, because the one path here that
    cannot be proved by rendering a page is the one that captures audio.
    The flags affect nothing but `getUserMedia`.
    """
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
    """The real threaded server, on a port nothing else is using."""
    cfg = WebUIConfig(host="127.0.0.1", port=_free_port(), token="")
    server = WebUIServer(cfg)
    server.start()
    ready = threading.Event()
    ready.wait(0.5)
    yield cfg.url
    server.stop()


@pytest.fixture(scope="module")
def standalone_served() -> str:
    """A control centre that deliberately has no daemon behind it."""
    cfg = WebUIConfig(
        host="127.0.0.1", port=_free_port(), token="", mode=WebUIMode.STANDALONE,
    )
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

    def test_legacy_llm_hash_is_canonicalised(self, page, served):
        page.goto(f"{served}/#/llm", wait_until="domcontentloaded")
        page.wait_for_selector("main h1", state="visible")

        assert page.evaluate("location.hash") == "#/llm-routes"
        assert page.locator("main h1").inner_text().strip()


class TestALongTableSaysThereIsMoreBelow:
    """A table taller than its container used to end in a sliced row.

    Cut through the middle of its own text with nothing pinned above it,
    that reads as a rendering fault rather than as more below.
    """

    def test_the_column_headings_stay_put_while_the_rows_scroll(self, page, served):
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                api.tools = async () => ({ servers: [], tools: Array.from(
                    { length: 60 },
                    (unused, n) => ({
                        name: `tool${n}`, origin: 'builtin', server: null,
                        description: 'does a thing', needs_confirmation: false,
                        last_use: null,
                    }),
                )});
            }"""
        )
        page.goto(f"{served}/#/tools")
        page.wait_for_selector("table th", state="visible")

        page.evaluate(
            "() => { const s = document.querySelector('.card .scroll');"
            "  s.scrollTop = s.scrollHeight; }"
        )
        page.wait_for_timeout(200)

        heading = page.locator("table th").first
        assert heading.is_visible()
        # Still inside the scroll region rather than scrolled off the top.
        assert heading.bounding_box()["y"] >= (
            page.locator(".card .scroll").bounding_box()["y"] - 1
        )
        assert not page.console_errors


class TestLlmRouteLayout:
    """The route view keeps every operational detail inside its own card."""

    def _open_with_routes(self, page, served):
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                const effective = {
                    fast: [{
                        active: true, blocked_until: null, failures: 1,
                        hits: 4, invalid: false, last_error: '',
                        masked_key: '••••••••LmVX', model: 'meta-llama/llama-prompt-guard-2-22m',
                        name: 'groq', tier: 'fast', provider: 'openai_compatible',
                        base_url: 'https://example.invalid/v1', api_key_env: 'GROQ_API_KEY',
                        timeout_sec: 2, enabled: false, capabilities: ['chat'],
                    }],
                    chat: [{
                        active: false, blocked_until: null, failures: 2,
                        hits: 12, invalid: false, last_error: 'rate limit reached',
                        masked_key: '••••••••kwrd', model: 'zai-glm-4.7',
                        name: 'cerebras', tier: 'chat', provider: 'openai_compatible',
                        local: false,
                    }],
                    private: [{
                        active: true, blocked_until: null, failures: 12,
                        hits: 28, invalid: false, last_error: '',
                        masked_key: '', model: 'qwen2.5:7b-ctx8k',
                        name: 'local-private', tier: 'private', provider: 'ollama',
                        local: true,
                    }],
                };
                const configured = [{
                    _index: 0, base_url_redacted: false,
                    name: 'groq', provider: 'openai_compatible',
                    base_url: 'https://example.invalid/v1', api_key: '••••••••LmVX',
                    api_key_env: 'GROQ_API_KEY',
                    model: 'meta-llama/llama-prompt-guard-2-22m', tier: 'fast',
                    timeout_sec: 2, enabled: false, capabilities: ['chat'],
                }];
                const routeFields = [
                    { key: 'name', label: 'Name', type: 'str', default: 'New route' },
                    { key: 'provider', label: 'Protocol', type: 'choice',
                      default: 'openai_compatible', choices: [
                        { value: 'openai_compatible', label: 'OpenAI-compatible' },
                        { value: 'ollama', label: 'Ollama' },
                        { value: 'claude_subscription', label: 'Claude subscription' },
                        { value: 'codex_subscription', label: 'Codex subscription' },
                        { value: 'crew_chat', label: 'Crew chat' },
                      ] },
                    { key: 'base_url', label: 'Base URL', type: 'str' },
                    { key: 'api_key', label: 'API Key', type: 'password', is_secret: true,
                      default: '' },
                    { key: 'api_key_env', label: 'API Key Environment', type: 'str',
                      default: '' },
                    { key: 'model', label: 'Model', type: 'str' },
                    { key: 'tier', label: 'Tier', type: 'choice', default: 'chat',
                      choices: [{ value: 'fast', label: 'Fast' }, { value: 'chat', label: 'Chat' }] },
                    { key: 'timeout_sec', label: 'Timeout', type: 'float', default: 4,
                      min: 0.1, max: 600, step: 0.5 },
                    { key: 'enabled', label: 'Enabled', type: 'bool', default: true },
                    { key: 'capabilities', label: 'Capabilities', type: 'list',
                      default: ['chat', 'stream', 'tools'] },
                ];
                api.llmRoutes = async () => ({
                    effective_chains: effective,
                    configured_routes: configured,
                    route_fields: routeFields,
                    provider_placeholders: {
                      openai_compatible: { base_url: 'https://provider.example/v1' },
                      ollama: { base_url: 'http://127.0.0.1:11434', model: 'qwen2.5:7b' },
                      claude_subscription: { base_url: 'claude-cli', model: 'claude-subscription' },
                      codex_subscription: { base_url: 'codex-cli', model: 'gpt-5.6-sol' },
                      crew_chat: { base_url: 'crew-chat', model: 'crew-chat' },
                    },
                    chat_backend_override: 'auto', crew_chat_agent: '',
                });
                api.saveLlmRoutes = async (routes) => {
                    window.__savedRoutes = routes;
                    return { configured_routes: routes, effective_chains: effective,
                             route_fields: routeFields, provider_placeholders: {} };
                };
            }"""
        )
        page.goto(f"{served}/#/llm-routes")
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

    def test_effective_routes_name_locality(self, page, served):
        self._open_with_routes(page, served)

        chat = page.locator(".llm-chain").nth(1)
        private = page.locator(".llm-chain").nth(2)
        assert chat.get_by_text("remote", exact=True).is_visible()
        assert private.get_by_text("local", exact=True).is_visible()

    def test_editor_keeps_every_operational_route_field(self, page, served):
        self._open_with_routes(page, served)

        route = page.locator(".route-config").first
        assert route.get_by_label("API Key Environment").input_value() == "GROQ_API_KEY"
        assert route.get_by_label("Enabled").is_checked() is False
        assert route.get_by_label("chat", exact=True).is_checked() is True
        assert route.get_by_label("stream", exact=True).is_checked() is False
        assert page.locator(".route-editor").count() == 0

    def test_schema_editor_round_trips_changes_without_rebuilding_runtime_routes(
        self, page, served,
    ):
        self._open_with_routes(page, served)
        route = page.locator(".route-config").first
        route.get_by_label("Name", exact=True).fill("renamed-groq")
        route.get_by_label("Enabled").check()
        route.get_by_label("stream", exact=True).check()
        page.get_by_role("button", name="Save routes").click()
        page.wait_for_function("window.__savedRoutes?.length === 1")

        saved = page.evaluate("window.__savedRoutes[0]")
        assert saved["_index"] == 0
        assert saved["name"] == "renamed-groq"
        assert saved["api_key"] == "••••••••LmVX"
        assert saved["enabled"] is True
        assert saved["capabilities"] == ["chat", "stream"]

    def test_provider_choice_updates_placeholders_and_chat_only_constraint(
        self, page, served,
    ):
        self._open_with_routes(page, served)
        page.get_by_role("button", name="+ Add route").click()
        added = page.locator(".route-config").last
        added.get_by_label("Protocol").select_option("codex_subscription")

        assert added.get_by_label("Base URL").get_attribute("placeholder") == "codex-cli"
        assert added.get_by_label("Model", exact=True).get_attribute("placeholder") == "gpt-5.6-sol"
        assert added.get_by_label("Tier").input_value() == "chat"
        assert added.get_by_label("Tier").is_disabled()


class TestSettingsCoherence:
    def test_real_settings_follow_the_input_and_output_pipeline(self, page, served):
        page.goto(f"{served}/#/settings", wait_until="networkidle")
        nav = page.locator(".settings-nav")

        assert nav.get_by_role("button", name="Local AI & Behaviour").is_visible()
        assert nav.get_by_role("button", name="Speech Input").is_visible()
        assert nav.get_by_role("button", name="Speech Recognition").is_visible()
        assert nav.get_by_role("button", name="Speech Output").is_visible()
        assert nav.get_by_role("button", name="Piper TTS").count() == 0

        nav.get_by_role("button", name="Local AI & Behaviour").click()
        assert page.get_by_role("heading", name="Local models").is_visible()
        assert page.get_by_role("heading", name="Timeouts").is_visible()
        assert page.get_by_role("heading", name="Thinking and behaviour").is_visible()
        assert page.get_by_role("link", name="Open LLM routes").is_visible()

        nav.get_by_role("button", name="Speech Recognition").click()
        assert page.get_by_role("heading", name="Whisper").is_visible()

        nav.get_by_role("button", name="Speech Output").click()
        for heading in ("Common output", "Cloud chain", "Piper", "Chatterbox", "Kokoro"):
            assert page.get_by_role("heading", name=heading, exact=True).is_visible()

    def test_cloud_provider_chain_is_editable_without_raw_json(self, page, served):
        payload = {
            "path": "C:/config.json", "daemon_running": True,
            "categories": [
                {
                    "key": "local_ai", "label": "Local AI & Behaviour",
                    "description": "Effective providers are configured in LLM Routes.",
                    "action_label": "Open LLM routes", "action_href": "#/llm-routes",
                },
                {"key": "speech_output", "label": "Speech Output"},
            ],
            "fields": [
                {
                    "key": "ollama_chat_model", "label": "Chat Model", "description": "Local fallback",
                    "category": "local_ai", "section": "Local models",
                    "type": "choice", "choices": [
                        {"value": "local-model", "label": "Local model"},
                    ], "value": "local-model", "restart_required": True,
                    "is_secret": False,
                },
                {
                    "key": "tts_cloud_providers", "label": "Cloud Provider Chain",
                    "description": "Ordered cloud voices", "category": "speech_output",
                    "section": "Cloud chain",
                    "type": "object_list", "restart_required": True,
                    "is_secret": False,
                    "value": [{
                        "name": "ElevenLabs", "provider": "elevenlabs",
                        "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "voice-1",
                        "model": "eleven_multilingual_v2", "enabled": True,
                        "timeout_sec": 8.5,
                    }],
                    "item_fields": [
                        {"key": "name", "label": "Name", "type": "str"},
                        {"key": "provider", "label": "Provider", "type": "str"},
                        {"key": "api_key_env", "label": "API Key Environment", "type": "str"},
                        {"key": "voice_id", "label": "Voice ID", "type": "str"},
                        {"key": "model", "label": "Model", "type": "str"},
                        {"key": "enabled", "label": "Enabled", "type": "bool"},
                        {"key": "timeout_sec", "label": "Timeout", "type": "float", "min": 0.1, "max": 600, "step": 0.5},
                    ],
                },
            ],
        }
        page.route("**/api/settings", lambda route: route.fulfill(json=payload))
        page.goto(f"{served}/#/settings", wait_until="domcontentloaded")
        page.wait_for_selector("main h1", state="visible")

        assert page.get_by_role("link", name="Open LLM routes").is_visible()
        page.get_by_role("button", name="Speech Output").click()

        assert page.locator(".object-item").count() == 1
        assert page.get_by_label("Name").input_value() == "ElevenLabs"
        assert page.locator(".route-editor").count() == 0

    def test_cloud_provider_controls_add_disable_reorder_and_save(self, page, served):
        payload = {
            "path": "C:/config.json", "daemon_running": True,
            "categories": [{"key": "speech_output", "label": "Speech Output"}],
            "fields": [{
                "key": "tts_cloud_providers", "label": "Cloud Provider Chain",
                "description": "Ordered cloud voices", "category": "speech_output",
                "section": "Cloud chain",
                "type": "object_list", "restart_required": True,
                "is_secret": False,
                "value": [{
                    "name": "ElevenLabs", "provider": "elevenlabs",
                    "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "voice-1",
                    "model": "eleven_multilingual_v2", "enabled": True,
                    "timeout_sec": 8.5,
                }],
                "item_fields": [
                    {"key": "name", "label": "Name", "type": "str", "default": "Cloud provider"},
                    {"key": "provider", "label": "Provider", "type": "choice",
                     "default": "fish_audio", "choices": [
                         {"value": "fish_audio", "label": "Fish Audio"},
                         {"value": "elevenlabs", "label": "ElevenLabs"},
                     ]},
                    {"key": "api_key_env", "label": "API Key Environment", "type": "str", "default": ""},
                    {"key": "voice_id", "label": "Voice ID", "type": "str", "default": ""},
                    {"key": "model", "label": "Model", "type": "str", "default": ""},
                    {"key": "enabled", "label": "Enabled", "type": "bool", "default": True},
                    {"key": "timeout_sec", "label": "Timeout", "type": "float",
                     "min": 0.1, "max": 600, "step": 0.5, "default": 10.0},
                ],
            }],
        }
        written = []

        def settings(route, request):
            if request.method == "PUT":
                written.append(request.post_data_json)
                route.fulfill(json={"written": ["tts_cloud_providers"],
                                    "restart_required": ["tts_cloud_providers"]})
            else:
                route.fulfill(json=payload)

        page.route("**/api/settings", settings)
        page.goto(f"{served}/#/settings", wait_until="domcontentloaded")
        page.wait_for_selector(".object-item", state="visible")
        page.get_by_role("button", name="+ Add provider").click()
        cards = page.locator(".object-item")
        assert cards.count() == 2
        assert cards.nth(1).locator("select").input_value() == "fish_audio"
        assert cards.nth(1).locator('input[type="checkbox"]').is_checked()
        assert cards.nth(1).locator('input[type="number"]').input_value() == "10"

        cards.nth(0).locator('input[type="checkbox"]').uncheck()
        cards.nth(0).get_by_role("button", name="Move down: ElevenLabs").click()
        page.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_timeout(100)

        providers = written[-1]["changes"]["tts_cloud_providers"]
        assert [provider["provider"] for provider in providers] == [
            "fish_audio", "elevenlabs",
        ]
        assert providers[1]["enabled"] is False


class TestStandaloneShell:
    def test_header_does_not_claim_a_live_session(self, browser, standalone_served):
        context = browser.new_context()
        page = context.new_page()
        page.goto(standalone_served, wait_until="networkidle")
        page.wait_for_function("document.querySelector('#phase-text').textContent.length > 0")

        assert page.locator("#phase-text").inner_text() == "not running"
        assert page.locator("#uptime-value").inner_text() == "—"
        assert page.locator("#passive-indicator").is_hidden()
        assert page.locator("#conversation-indicator").is_hidden()
        context.close()

    def test_footer_names_the_local_resident_model(self, browser, standalone_served):
        context = browser.new_context()
        page = context.new_page()
        page.route("**/api/system", lambda route: route.fulfill(json={
            "gpu": {"used_mb": 6080, "total_mb": 7960},
            "models": {
                "chat": "gpt-oss-120b", "provider": "openai_compatible",
                "loaded": [{"name": "qwen2.5:7b-ctx8k"}],
            },
        }))
        page.goto(standalone_served, wait_until="networkidle")
        page.wait_for_function("document.querySelector('#foot-local-model')?.textContent.includes('qwen')")

        footer = page.locator("#sidebar-foot").inner_text()
        assert "qwen2.5:7b-ctx8k" in footer
        assert "gpt-oss-120b" not in footer
        assert "5.94 GB / 7.77 GB" in footer
        context.close()


class TestSystemModelTruth:
    def test_remote_effective_local_fallback_and_residency_are_distinct(self, page, served):
        page.route("**/api/system", lambda route: route.fulfill(json={
            "gpu": {"name": "GPU", "used_mb": 4096, "total_mb": 8192},
            "models": {
                "effective": {
                    "fast": {"name": "cloud-fast", "model": "remote-fast",
                             "provider": "openai_compatible", "location": "remote"},
                    "chat": {"name": "cloud-chat", "model": "remote-chat",
                             "provider": "openai_compatible", "location": "remote"},
                },
                "local": {
                    "fast_fallback": {"model": "local-fast", "provider": "ollama"},
                    "chat_fallback": {"model": "local-chat", "provider": "ollama"},
                    "private": {"model": "local-chat", "provider": "ollama"},
                    "embedding": {"model": "local-embed", "provider": "ollama"},
                },
                "resident": [{"name": "resident-now", "size": "4 GB",
                              "processor": "100% GPU", "context": "4096",
                              "until": "4 minutes"}],
            },
            "ollama_environment": {},
            "speech_recognition": {}, "speech_output": {}, "paths": [],
            "process": {"pid": 1, "python": "3", "platform": "test", "threads": 1},
        }))
        page.route("**/api/turns?*", lambda route: route.fulfill(json={"turns": []}))
        page.goto(f"{served}/#/system", wait_until="networkidle")

        effective = page.locator(".effective-models")
        local = page.locator(".local-models")
        resident = page.locator(".resident-models")
        assert "remote-chat" in effective.inner_text()
        assert "remote" in effective.inner_text()
        assert "local-fast" in local.inner_text()
        assert "local-embed" in local.inner_text()
        assert "resident-now" in resident.inner_text()
        assert "4 GB / 8 GB" in resident.inner_text()
        assert "remote-chat" not in resident.inner_text()


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


class TestMotionIsOptional:
    """Nothing that moves is load-bearing.

    Mission Control uses motion for two things: a heartbeat on the reading
    and a one-shot mark on a line that has just arrived. Both are read
    somewhere else in text as well, so switching them off costs nothing.
    Asserting it here keeps a later animation from sneaking past the rule.
    """

    def _animations(self, browser, served, **context_args):
        context = browser.new_context(**context_args)
        page = context.new_page()
        # The test server has no crew endpoint, and a state the view reports
        # as unreachable has nothing to beat for.
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                api.crew = async () => ({
                    configured: true, reachable: true, checked_at: 1755800000,
                    entries: [], agents: [], daily: [],
                });
            }"""
        )
        page.goto(f"{served}/#/crew")
        page.wait_for_selector(".state-pill", state="visible")
        try:
            return page.evaluate(
                """() => [...document.querySelectorAll('*')]
                    .map(node => getComputedStyle(node).animationName)
                    .filter(name => name && name !== 'none')"""
            )
        finally:
            context.close()

    def test_a_reader_who_asked_for_less_motion_gets_none(self, browser, served):
        moving = self._animations(browser, served, reduced_motion="reduce")

        assert moving == [], f"still animating: {sorted(set(moving))}"

    def test_and_a_reader_who_did_not_still_sees_the_heartbeat(self, browser, served):
        moving = self._animations(browser, served, reduced_motion="no-preference")

        assert "breathe" in moving


class TestCalendarDaysKeepTheirName:
    """A day is not an instant.

    The diary, the passive record, and the crew's activity all group by
    calendar day and hand the interface a bare `YYYY-MM-DD`. Read as an
    instant that is UTC midnight and then printed in local time, every one
    of those days is renamed to the day before for anyone west of
    Greenwich. A full timestamp is an instant and must still be converted.
    """

    def _in_timezone(self, browser, served, zone):
        context = browser.new_context(timezone_id=zone)
        page = context.new_page()
        page.goto(served, wait_until="networkidle")
        try:
            return page.evaluate(
                """async () => {
                    const fmt = await import('/static/js/fmt.js');
                    return {
                        bare: fmt.date('2026-08-22'),
                        instant: fmt.date('2026-08-22T03:00:00Z'),
                    };
                }"""
            )
        finally:
            context.close()

    def test_a_bare_day_reads_the_same_everywhere(self, browser, served):
        west = self._in_timezone(browser, served, "America/Los_Angeles")
        east = self._in_timezone(browser, served, "Asia/Tokyo")

        assert "22" in west["bare"], f"the day was renamed: {west['bare']}"
        assert west["bare"] == east["bare"]

    def test_a_full_timestamp_is_still_converted(self, browser, served):
        west = self._in_timezone(browser, served, "America/Los_Angeles")

        # 03:00 UTC is the previous evening in Los Angeles.
        assert "21" in west["instant"], f"an instant stopped converting: {west['instant']}"


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

        assert page.locator(".state-pill").inner_text().strip()
        assert page.locator(".state-pill-age").inner_text().strip()

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


class TestTheOverviewLeadsSomewhere:
    """The landing page reads across the others, so it links into them.

    It stopped keeping its own list of recent turns: the Conversation view
    shows the same history as a conversation, and a worse copy of it here
    only teaches a reader that the two disagree.
    """

    def _turn(self, turn_id, at, total_ms):
        return {
            "turn_id": turn_id, "started_at": at, "source": "voice",
            "language": "de", "total_ms": total_ms,
            "transcript": f"question {turn_id}", "reply": f"answer {turn_id}",
            "error": None, "tools": [],
            "stages": [{"name": "stt", "duration_ms": total_ms * 0.2},
                       {"name": "llm", "duration_ms": total_ms * 0.6}],
        }

    def _open(self, page, served):
        turns = [self._turn(n, 1_755_800_000 + n * 60, total)
                 for n, total in enumerate([1800, 2000, 2200, 9000], start=1)]
        page.goto(f"{served}/#/logs", wait_until="networkidle")
        page.evaluate(
            """async (turns) => {
                const { api } = await import('/static/js/api.js');
                api.turns = async () => ({ turns });
                api.status = async () => ({
                    phase: 'idle', phase_since: 1755800000, uptime_seconds: 900,
                    last_turn: turns[turns.length - 1], discarded: { no_speech: 2 },
                    models: { chat: 'qwen2.5:7b-ctx8k' },
                });
                api.tools = async () => ({ tools: [
                    { name: 'getWeather', origin: 'builtin' },
                    { name: 'askCrew', origin: 'builtin' },
                ], servers: [] });
                api.security = async () => ({ level: 'critical', pending: [] });
                api.graphStats = async () => ({ total_nodes: 42, total_tokens: 1337 });
            }""",
            turns,
        )
        page.goto(f"{served}/#/overview")
        page.wait_for_selector(".readings", state="visible")
        return turns

    def test_it_no_longer_keeps_its_own_list_of_recent_turns(self, page, served):
        self._open(page, served)

        assert page.get_by_text("question 1", exact=True).count() == 0
        assert not page.console_errors

    def test_the_typical_wait_is_shown_beside_the_last_one(self, page, served):
        """One slow turn is not the state of things."""
        self._open(page, served)

        # Totals are 1.80, 2.00, 2.20 and 9.00 seconds: the median is 2.10.
        assert "2.10 s" in page.locator(".turn-typical").inner_text()
        assert "9.00 s" in page.locator(".turn-total-big").inner_text()

    def test_every_reading_leads_to_the_view_that_holds_it(self, page, served):
        self._open(page, served)

        targets = page.locator(".readings a.card").evaluate_all(
            "cards => cards.map(card => new URL(card.href).hash)"
        )

        assert targets == ["#/memory", "#/tools", "#/security", "#/conversation"]

    def test_following_a_reading_opens_its_view(self, page, served):
        self._open(page, served)

        page.locator(".readings a.card").first.click()
        page.wait_for_timeout(300)

        assert page.evaluate("location.hash") == "#/memory"
        assert not page.console_errors


class TestTheConversationIsTheConversationView:
    """The exchange is what this view is for.

    It reads live because the daemon already publishes what it is doing and
    how long it has been doing it. Nothing here invents a state it cannot
    see, and nothing that moves is the only thing saying what it says.
    """

    def _turn(self, turn_id, at, said, replied, total_ms=1900.0):
        return {
            "turn_id": turn_id, "started_at": at, "source": "voice",
            "language": "de", "language_probability": 0.98,
            "total_ms": total_ms, "transcript": said, "reply": replied,
            "error": None, "tools": [],
            "stages": [{"name": "stt", "duration_ms": 400.0},
                       {"name": "llm", "duration_ms": 1500.0}],
        }

    TURNS = None

    def _open(self, page, served, turns=None):
        turns = turns if turns is not None else [
            self._turn(1, 1_755_800_000, "Wie ist das Wetter", "Vierzehn Grad und bewölkt."),
            self._turn(2, 1_755_800_100, "Und morgen", "Morgen wird es trocken."),
        ]
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async (turns) => {
                const { api } = await import('/static/js/api.js');
                api.conversation = async () => ({
                    turns, discarded: {}, conversation_mode: false,
                });
                api.voiceStatus = async () => ({ ingress: false, sample_rate: 16000 });
                window.__push = async (kind, data) => {
                    const { live } = await import('/static/js/sse.js');
                    live._emit(kind, data);
                };
            }""",
            turns,
        )
        page.goto(f"{served}/#/conversation")
        page.wait_for_selector(".dialogue", state="visible")
        return turns

    def test_the_exchange_is_the_first_thing_you_see(self, page, served):
        """It used to sit below the passive record, thousands of pixels down."""
        self._open(page, served)

        top = page.locator(".dialogue").bounding_box()["y"]

        assert top < 800, f"the conversation starts {top}px down the page"
        assert not page.console_errors

    def test_the_exchange_is_the_only_thing_that_scrolls(self, page, served):
        """Two nested scrollers means every gesture has two answers."""
        self._open(page, served)

        page_scrolls = page.evaluate(
            "() => document.documentElement.scrollHeight > window.innerHeight + 1"
        )

        assert not page_scrolls
        assert page.evaluate(
            "() => { const d = document.querySelector('.dialogue');"
            "  return getComputedStyle(d).overflowY; }"
        ) == "auto"

    def test_the_band_follows_the_phase_the_daemon_publishes(self, page, served):
        self._open(page, served)

        page.evaluate(
            """() => window.__push('phase', {
                phase: 'thinking', phase_since: Date.now() / 1000, phase_seconds: 0,
            })"""
        )
        page.wait_for_timeout(200)

        assert "thinking" in page.locator(".voice-phase").inner_text()
        assert page.locator(".voice-phase-dot").get_attribute("data-phase") == "thinking"

    def test_the_band_names_the_stage_a_turn_has_reached(self, page, served):
        self._open(page, served)

        page.evaluate(
            """() => {
                window.__push('phase', {
                    phase: 'tool', phase_since: Date.now() / 1000, phase_seconds: 0,
                });
                window.__push('stage', {
                    turn_id: 9, stage: 'tool:getWeather', elapsed_ms: 820,
                });
            }"""
        )
        page.wait_for_timeout(200)

        assert "tool:getWeather" in page.locator(".voice-phase").inner_text()

    def test_a_turn_reads_as_an_exchange_rather_than_a_row(self, page, served):
        self._open(page, served)

        assert page.locator(".turn").count() == 2
        assert page.get_by_text("Wie ist das Wetter", exact=True).is_visible()
        assert page.get_by_text("Vierzehn Grad und bewölkt.", exact=True).is_visible()
        assert page.locator(".turn-said").count() == 2
        assert page.locator(".turn-reply").count() == 2

    def test_the_newest_turn_sits_at_the_bottom_next_to_the_composer(self, page, served):
        self._open(page, served)

        said = page.locator(".turn-said").all_inner_texts()

        assert said == ["Wie ist das Wetter", "Und morgen"]

    def test_only_a_turn_that_arrived_while_watching_is_marked(self, page, served):
        turns = self._open(page, served)
        # A first load marks nothing, or everything would be marked.
        assert page.locator(".turn.arrived").count() == 0

        landed = self._turn(3, 1_755_800_200, "Danke", "Gern geschehen.")
        page.evaluate(
            """async ([turns, landed]) => {
                const { api } = await import('/static/js/api.js');
                api.conversation = async () => ({
                    turns: [...turns, landed], discarded: {}, conversation_mode: false,
                });
                window.__push('turn', landed);
            }""",
            [turns, landed],
        )
        page.wait_for_timeout(400)

        assert page.locator(".turn").count() == 3
        assert page.locator(".turn.arrived").count() == 1

    def test_what_was_said_is_rendered_as_text(self, page, served):
        self._open(page, served, [
            self._turn(1, 1_755_800_000,
                       "<script>alert(1)</script> read my notes",
                       "<img src=x onerror=alert(1)> here they are"),
        ])

        assert page.get_by_text(
            "<script>alert(1)</script> read my notes", exact=True
        ).is_visible()
        assert page.locator("main img").count() == 0
        assert page.locator("main script").count() == 0

    def test_the_microphone_says_in_words_what_the_meter_shows(self, page, served):
        """A reader who wants no motion still learns whether it is capturing."""
        self._open(page, served)

        assert page.locator(".voice-mic-state").inner_text().strip()

    def test_a_reader_who_asked_for_less_motion_gets_no_level_meter(
        self, browser, served,
    ):
        for preference, expected in (("reduce", False), ("no-preference", True)):
            context = browser.new_context(reduced_motion=preference)
            opened = context.new_page()
            opened.goto(served, wait_until="networkidle")
            try:
                allowed = opened.evaluate(
                    """async () => {
                        const ui = await import('/static/js/ui.js');
                        return ui.motionAllowed();
                    }"""
                )
                assert allowed is expected, preference
            finally:
                context.close()


class TestTheMicrophoneReachesTheDaemon:
    """The one path here that rendering a page cannot prove.

    Everything else on the Conversation view is a reading being painted.
    This is a capture opening, a socket carrying frames to the listener's
    own ingress, and a level measured from those same frames. It runs
    against the real server with a synthetic microphone, because a stub
    would prove only that the stub was called.
    """

    def _open(self, browser, served):
        pytest.importorskip("simple_websocket")
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        page.console_errors = []
        page.on("pageerror", lambda error: page.console_errors.append(str(error)))
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        # Standalone nothing is listening, so the view would refuse to open
        # the microphone at all. The socket it opens is the real one.
        page.evaluate(
            """async () => {
                const { api } = await import('/static/js/api.js');
                api.voiceStatus = async () => ({ ingress: true, sample_rate: 16000 });
            }"""
        )
        page.goto(f"{served}/#/conversation")
        page.wait_for_selector(".voice-mic", state="visible")
        return context, page

    def test_opening_it_streams_and_the_level_follows_what_it_hears(
        self, browser, served,
    ):
        context, page = self._open(browser, served)
        try:
            page.locator(".voice-mic").click()
            page.wait_for_timeout(2500)

            assert page.locator(".voice-mic").get_attribute("aria-pressed") == "true"
            # The state is in words, not only in the meter.
            assert page.locator(".voice-mic-state").inner_text().strip()
            height = page.evaluate(
                "() => document.querySelector('.voice-level > span').style.height"
            )
            assert height not in ("", "0%"), f"no level from a live capture: {height!r}"
            assert not page.console_errors
        finally:
            context.close()

    def test_leaving_the_view_lets_the_microphone_go(self, browser, served):
        """A capture the user cannot see is a capture they cannot stop."""
        context, page = self._open(browser, served)
        try:
            page.locator(".voice-mic").click()
            page.wait_for_timeout(1500)

            page.goto(f"{served}/#/overview")
            page.wait_for_timeout(800)

            page.goto(f"{served}/#/conversation")
            page.wait_for_selector(".voice-mic", state="visible")
            assert page.locator(".voice-mic").get_attribute("aria-pressed") == "false"
            assert not page.console_errors
        finally:
            context.close()


class TestPassiveRecordHasItsOwnHome:
    """The record of everything overheard is not the conversation.

    It is a privacy surface with its own switch and its own delete paths,
    and it grows without limit. Left on the Conversation view it pushed the
    exchange itself off the bottom of the page, which is the one thing that
    view exists to show.
    """

    LINES = [
        {"id": 3, "date_utc": "2026-08-22", "ts_utc": "2026-08-22T09:15:00Z",
         "text": "<img src=x onerror=alert(1)> the kettle is on", "addressed": 0},
        {"id": 2, "date_utc": "2026-08-22", "ts_utc": "2026-08-22T09:05:00Z",
         "text": "Jarvis, what is the time", "addressed": 1},
        {"id": 1, "date_utc": "2026-08-21", "ts_utc": "2026-08-21T20:00:00Z",
         "text": "someone said something yesterday", "addressed": 0},
    ]

    def _open(self, page, served, enabled=False):
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async ([lines, enabled]) => {
                const { api } = await import('/static/js/api.js');
                window.__switched = [];
                api.passive = async () => ({
                    lines, enabled, undigested_count: 2, llm_provider: 'ollama',
                });
                api.setPassiveEnabled = async (next) => {
                    window.__switched.push(next);
                    return { enabled: next };
                };
            }""",
            [self.LINES, enabled],
        )
        page.goto(f"{served}/#/passive")
        page.wait_for_selector("main h1", state="visible")

    def test_the_conversation_view_no_longer_carries_the_record(self, page, served):
        page.goto(f"{served}/#/conversation", wait_until="networkidle")
        page.wait_for_selector("main h1", state="visible")
        page.wait_for_timeout(400)

        assert page.locator(".passive-day").count() == 0
        assert not page.console_errors

    def test_the_record_groups_what_was_heard_by_day(self, page, served):
        self._open(page, served)

        assert page.locator(".passive-day").count() == 2
        assert page.locator(".passive-line").count() == 3
        assert not page.console_errors

    def test_overheard_text_is_rendered_as_text(self, page, served):
        self._open(page, served)

        assert page.get_by_text(
            "<img src=x onerror=alert(1)> the kettle is on", exact=True
        ).is_visible()
        assert page.locator("main img").count() == 0

    def test_a_line_addressed_to_jarvis_carries_a_named_mark(self, page, served):
        self._open(page, served)

        assert page.locator(".passive-line.addressed").count() == 1
        # A colour alone is not a marker: the mark says what it means.
        assert page.get_by_label("addressed to Jarvis").count() == 1

    def test_turning_the_record_on_asks_before_it_starts_listening(self, page, served):
        self._open(page, served, enabled=False)
        asked = []
        page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))

        page.get_by_role("button", name="Turn on").click()
        page.wait_for_timeout(200)

        assert asked, "the switch started the record without asking"
        assert "ollama" in asked[0]
        assert page.evaluate("window.__switched") == []

    def test_turning_the_record_off_needs_no_permission(self, page, served):
        self._open(page, served, enabled=True)

        page.get_by_role("button", name="Turn off").click()
        page.wait_for_timeout(200)

        assert page.evaluate("window.__switched") == [False]


class TestTheDiagnosticLogIsReadable:
    """The log is the surface for "what did it actually do".

    It carries a timestamp, a category and a message, and nothing else. It
    has no severity, so the view invents none: what it can sort by is the
    category, and what it can narrow by is the text.
    """

    ENTRIES = [
        {"timestamp": 1_755_800_000, "category": "voice",
         "message": "voice socket opened"},
        {"timestamp": 1_755_800_001, "category": "webui",
         "message": "crew poller reading every 10s"},
        {"timestamp": 1_755_800_002, "category": "tts",
         "message": "Voice language resolved: German"},
        {"timestamp": 1_755_800_003, "category": "voice",
         "message": "<script>alert(1)</script> frames accepted"},
    ]

    def _open(self, page, served, entries=None):
        page.goto(f"{served}/#/overview", wait_until="networkidle")
        page.evaluate(
            """async (entries) => {
                const { api } = await import('/static/js/api.js');
                window.__entries = entries;
                api.logs = async () => ({ entries: window.__entries });
            }""",
            self.ENTRIES if entries is None else entries,
        )
        page.goto(f"{served}/#/logs")
        page.wait_for_selector(".log-line", state="visible")

    def test_every_entry_is_a_row_rather_than_one_block_of_text(self, page, served):
        self._open(page, served)

        assert page.locator(".log-line").count() == 4
        assert page.locator("pre").count() == 0
        assert not page.console_errors

    def test_the_categories_offered_are_the_ones_actually_in_the_log(
        self, page, served,
    ):
        self._open(page, served)

        offered = page.locator(".log-filters .chip").all_inner_texts()

        assert offered[0] == "All"
        assert set(offered[1:]) == {"voice", "webui", "tts"}

    def test_choosing_a_category_narrows_the_log(self, page, served):
        self._open(page, served)

        page.get_by_role("button", name="voice", exact=True).click()
        page.wait_for_timeout(200)

        assert page.locator(".log-line").count() == 2
        assert set(page.locator(".log-category").all_inner_texts()) == {"voice"}

    def test_searching_narrows_the_log_and_says_how_much_is_showing(
        self, page, served,
    ):
        self._open(page, served)

        page.fill(".log-search", "German")
        page.wait_for_timeout(300)

        assert page.locator(".log-line").count() == 1
        assert "1" in page.locator(".log-shown").inner_text()

    def test_a_search_that_matches_nothing_says_so(self, page, served):
        self._open(page, served)

        page.fill(".log-search", "nothing whatsoever")
        page.wait_for_timeout(300)

        assert page.locator(".log-line").count() == 0
        assert page.get_by_text("Nothing here matches the filter.").is_visible()

    def test_what_was_logged_is_rendered_as_text(self, page, served):
        self._open(page, served)

        assert page.get_by_text(
            "<script>alert(1)</script> frames accepted", exact=True
        ).is_visible()
        assert page.locator("main script").count() == 0

    def test_following_keeps_the_newest_entry_in_view(self, page, served):
        many = [
            {"timestamp": 1_755_800_000 + n, "category": "voice",
             "message": f"entry number {n}"}
            for n in range(120)
        ]
        self._open(page, served, many)
        page.wait_for_timeout(300)

        at_bottom = page.evaluate(
            """() => { const w = document.querySelector('.log-well');
                 return w.scrollHeight - w.scrollTop - w.clientHeight < 40; }"""
        )

        assert at_bottom, "the newest entry is not in view while following"

    def test_turning_following_off_leaves_the_log_where_you_put_it(
        self, page, served,
    ):
        many = [
            {"timestamp": 1_755_800_000 + n, "category": "voice",
             "message": f"entry number {n}"}
            for n in range(120)
        ]
        self._open(page, served, many)
        page.get_by_role("button", name="Follow").click()
        page.evaluate("() => { document.querySelector('.log-well').scrollTop = 0; }")

        # A new entry lands and the poll picks it up.
        page.evaluate(
            """() => window.__entries = [...window.__entries, {
                timestamp: 1755800200, category: 'voice', message: 'entry number 999',
            }]"""
        )
        page.wait_for_timeout(2600)

        assert page.evaluate("() => document.querySelector('.log-well').scrollTop") < 40
        assert page.locator(".log-line").count() == 121


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
