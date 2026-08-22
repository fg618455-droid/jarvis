from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis.security.gate import SecurityGate
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.browser_interact import (
    BROWSER_ACTIONS,
    BrowserController,
    BrowserInteractTool,
    InvalidBrowserReference,
    _browser_action,
)
from jarvis.tools.types import ToolErrorCode


def _ctx(cfg) -> ToolContext:
    return ToolContext(None, cfg, "", "", "", 0, lambda _message: None)


class _Page:
    def __init__(self) -> None:
        self.url = "https://example.test/start"
        self.mouse = MagicMock()
        self.go_back = MagicMock()
        self.body = MagicMock()
        self.body.inner_text.return_value = "Visible page text"
        self.body.aria_snapshot.return_value = "- document: Visible page text"
        self.actions = MagicMock()
        self.actions.count.return_value = 2
        self.first = MagicMock()
        self.first.is_visible.return_value = True
        self.first.get_attribute.side_effect = lambda name: {
            "aria-label": "Read more",
            "href": "/more",
            "type": None,
            "autocomplete": None,
            "role": "link",
        }.get(name)
        self.first.inner_text.return_value = "Read more"
        self.second = MagicMock()
        self.second.is_visible.return_value = True
        self.second.get_attribute.side_effect = lambda name: {
            "aria-label": "Email",
            "href": None,
            "type": "email",
            "autocomplete": "email",
            "role": "textbox",
        }.get(name)
        self.second.inner_text.return_value = ""
        self.actions.nth.side_effect = lambda index: [self.first, self.second][index]

    def locator(self, selector: str):
        if selector == "body":
            return self.body
        return self.actions


def _ready_controller() -> tuple[BrowserController, _Page]:
    controller = BrowserController()
    page = _Page()
    controller._page = page
    controller._browser = MagicMock()
    controller._playwright = MagicMock()
    return controller, page


def test_browser_action_surface_is_exactly_the_bounded_semantic_set() -> None:
    assert BROWSER_ACTIONS == {
        "browser_open", "browser_snapshot", "browser_click", "browser_fill",
        "browser_scroll", "browser_read", "browser_back", "browser_close",
    }


def test_browser_open_uses_a_headed_isolated_context() -> None:
    controller = BrowserController()
    playwright = MagicMock()
    browser = playwright.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    controller._playwright_factory = lambda: playwright

    result = controller.browser_open("https://example.test/watch")

    playwright.chromium.launch.assert_called_once_with(headless=False)
    browser.new_context.assert_called_once_with()
    page.goto.assert_called_once_with("https://example.test/watch", wait_until="domcontentloaded")
    assert result["url"] == "https://example.test/watch"


@pytest.mark.parametrize("url", [
    "file:///C:/secret.txt", "javascript://alert(1)", "data://text/plain,x",
])
def test_browser_open_refuses_non_http_schemes_without_starting_playwright(url: str) -> None:
    controller = BrowserController()
    factory = MagicMock()
    controller._playwright_factory = factory

    with pytest.raises(ValueError):
        controller.browser_open(url)

    factory.assert_not_called()


def test_browser_snapshot_returns_visible_content_and_fresh_semantic_refs() -> None:
    controller, _page = _ready_controller()

    snapshot = controller.browser_snapshot()

    assert snapshot["text"] == "Visible page text"
    assert snapshot["accessibility_tree"].startswith("- document")
    assert [item["name"] for item in snapshot["controls"]] == ["Read more", "Email"]
    assert all(item["ref"].startswith("b1-") for item in snapshot["controls"])


def test_a_new_snapshot_invalidates_the_previous_refs() -> None:
    controller, _page = _ready_controller()
    first_ref = controller.browser_snapshot()["controls"][0]["ref"]
    controller.browser_snapshot()

    with pytest.raises(InvalidBrowserReference):
        controller.browser_click(first_ref)


def test_browser_click_uses_only_a_ref_from_the_latest_snapshot() -> None:
    controller, page = _ready_controller()
    ref = controller.browser_snapshot()["controls"][0]["ref"]

    controller.browser_click(ref)

    page.first.click.assert_called_once_with()


def test_browser_fill_uses_only_a_ref_from_the_latest_snapshot() -> None:
    controller, page = _ready_controller()
    ref = controller.browser_snapshot()["controls"][1]["ref"]

    controller.browser_fill(ref, "person@example.test")

    page.second.fill.assert_called_once_with("person@example.test")


def test_browser_scroll_uses_mouse_wheel_without_coordinates() -> None:
    controller, page = _ready_controller()

    controller.browser_scroll("down", 3)

    page.mouse.wheel.assert_called_once_with(0, 1800)


def test_browser_read_reads_the_page_or_a_snapshot_ref() -> None:
    controller, page = _ready_controller()
    ref = controller.browser_snapshot()["controls"][0]["ref"]
    page.first.inner_text.return_value = "Linked article"

    assert controller.browser_read() == "Visible page text"
    assert controller.browser_read(ref) == "Linked article"


def test_browser_back_delegates_to_page_history() -> None:
    controller, page = _ready_controller()

    controller.browser_back()

    page.go_back.assert_called_once_with(wait_until="domcontentloaded")


def test_browser_close_closes_context_browser_and_playwright() -> None:
    controller, _page = _ready_controller()
    context = MagicMock()
    controller._context = context
    browser = controller._browser
    playwright = controller._playwright

    controller.browser_close()

    context.close.assert_called_once_with()
    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()
    assert controller._page is None


class _FakeBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refs = {"b1-1": {"name": "Send message", "domain": "example.test", "sensitive": False}}

    def describe_ref(self, ref: str) -> dict:
        if ref not in self.refs:
            raise InvalidBrowserReference(ref)
        return self.refs[ref]

    def dispatch(self, kind: str, args: dict):
        self.calls.append((kind, args))
        if kind == "browser_snapshot":
            return {"url": "https://example.test", "text": "page", "controls": []}
        return {"ok": True}


def _resolver(*decisions):
    remaining = list(decisions)

    def resolve(*_args, **_kwargs):
        return remaining.pop(0)

    return resolve


def test_browser_tool_confirms_a_consequential_click_with_concrete_context(mock_config) -> None:
    browser = _FakeBrowser()
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or True
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = BrowserInteractTool(
        controller=browser,
        resolver=_resolver(
            {"kind": "browser_click", "args": {"ref": "b1-1"}, "risk": "consequential"},
            {"kind": "done", "args": {"summary": "Sent."}, "risk": "read_only"},
        ),
    )

    result = tool.run({"task": "Send the message"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.success is True
    assert channel.requests == [("browserInteract.browser_click", {
        "control": "Send message", "domain": "example.test", "task": "Send the message",
    })]
    assert browser.calls[0][0] == "browser_snapshot"
    assert browser.calls[1] == ("browser_click", {"ref": "b1-1"})


def test_browser_tool_stops_when_action_confirmation_is_declined(mock_config) -> None:
    browser = _FakeBrowser()
    channel = SimpleNamespace(is_available=True, ask=lambda *_args: False)
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = BrowserInteractTool(
        controller=browser,
        resolver=_resolver({"kind": "browser_fill", "args": {"ref": "b1-1", "text": "hello"}, "risk": "consequential"}),
    )

    result = tool.run({"task": "Type hello"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.success is False
    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert not any(kind == "browser_fill" for kind, _args in browser.calls)


def test_browser_tool_refuses_secret_fields_without_offering_confirmation(mock_config) -> None:
    browser = _FakeBrowser()
    browser.refs["b1-1"]["sensitive"] = True
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or True
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = BrowserInteractTool(
        controller=browser,
        resolver=_resolver({"kind": "browser_fill", "args": {"ref": "b1-1", "text": "secret"}, "risk": "secret"}),
    )

    result = tool.run({"task": "Enter the password"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.success is False
    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert channel.requests == []
    assert not any(kind == "browser_fill" for kind, _args in browser.calls)


def test_browser_tool_enforces_its_action_cap(mock_config) -> None:
    browser = _FakeBrowser()
    tool = BrowserInteractTool(
        controller=browser,
        resolver=lambda *_args, **_kwargs: {"kind": "browser_scroll", "args": {"direction": "down", "amount": 1}, "risk": "read_only"},
        max_actions=2,
    )

    result = tool.run({"task": "Keep scrolling"}, _ctx(mock_config))

    assert result.success is False
    assert result.error_code == ToolErrorCode.TIMEOUT.value
    assert [kind for kind, _args in browser.calls].count("browser_scroll") == 2


def test_browser_tool_confirms_navigation_to_a_domain_the_task_did_not_name(mock_config) -> None:
    browser = _FakeBrowser()
    browser.dispatch = MagicMock(side_effect=[{"state": "no browser"}, {"url": "https://other.test"}, {"state": "page"}])
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or False
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = BrowserInteractTool(
        controller=browser,
        resolver=_resolver({"kind": "browser_open", "args": {"url": "https://other.test"}, "risk": "ordinary"}),
    )

    result = tool.run({"task": "Find an article about trees"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert channel.requests[0][0] == "browserInteract.browser_open"


def test_browser_action_validator_rejects_unknown_risk_and_forbidden_fields() -> None:
    assert _browser_action({
        "kind": "browser_click", "args": {"ref": "b1-1"}, "risk": "unguarded",
    }) is None
    assert _browser_action({
        "kind": "browser_click", "args": {"ref": "b1-1", "selector": "#buy"}, "risk": "ordinary",
    }) is None


def test_browser_public_schema_has_only_task_and_routes_open_plus_interact_to_it() -> None:
    tool = BrowserInteractTool(controller=_FakeBrowser(), resolver=_resolver())

    assert set(tool.inputSchema["properties"]) == {"task"}
    assert tool.inputSchema["required"] == ["task"]
    assert "openOnComputer" in tool.description
    assert "opening and interacting" in tool.description
