from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis.security.gate import SecurityGate
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.desktop_interact import (
    DESKTOP_ACTIONS,
    DesktopController,
    DesktopInteractTool,
    ElevatedWindowError,
    InvalidDesktopReference,
    TabScopeError,
    _desktop_action,
)
from jarvis.tools.types import ToolErrorCode


def _ctx(cfg) -> ToolContext:
    return ToolContext(None, cfg, "", "", "", 0, lambda _message: None)


def _wrapper(name: str, control_type: str, automation_id: str = ""):
    wrapper = MagicMock()
    wrapper.window_text.return_value = name
    wrapper.element_info.name = name
    wrapper.element_info.control_type = control_type
    wrapper.element_info.automation_id = automation_id
    wrapper.element_info.process_id = 123
    wrapper.element_info.handle = hash((name, control_type)) & 0xFFFF
    wrapper.element_info.is_password = False
    return wrapper


def _ready_controller() -> tuple[DesktopController, MagicMock, MagicMock]:
    window = _wrapper("Notepad", "Window")
    button = _wrapper("Save", "Button", "saveButton")
    edit = _wrapper("Document", "Edit", "editor")
    window.descendants.return_value = [button, edit]
    controller = DesktopController(clock=lambda: 100.0)
    controller._desktop_factory = lambda: SimpleNamespace(windows=lambda: [window])
    controller._elevation_checker = lambda _pid: False
    return controller, window, button


def _tabbed_controller() -> tuple[DesktopController, MagicMock, MagicMock, MagicMock]:
    window = _wrapper("Notepad", "Window")
    tab_strip = _wrapper("", "Tab")
    first_tab = _wrapper("API notes", "TabItem", "tab-1")
    second_tab = _wrapper("Shopping list", "TabItem", "tab-2")
    first_tab.is_selected.return_value = True
    second_tab.is_selected.return_value = False
    edit = _wrapper("Text editor", "Edit", "editor")
    window.descendants.return_value = [tab_strip, first_tab, second_tab, edit]
    controller = DesktopController(clock=lambda: 100.0)
    controller._desktop_factory = lambda: SimpleNamespace(windows=lambda: [window])
    controller._elevation_checker = lambda _pid: False
    return controller, window, first_tab, second_tab


def test_desktop_action_surface_is_exactly_the_bounded_uia_set() -> None:
    assert DESKTOP_ACTIONS == {
        "desktop_list_windows", "desktop_inspect", "desktop_find",
        "desktop_invoke", "desktop_set_text", "desktop_select",
        "desktop_toggle", "desktop_scroll", "desktop_read",
    }


def test_desktop_list_windows_returns_scoped_ids_not_raw_handles() -> None:
    controller, _window, _button = _ready_controller()

    windows = controller.desktop_list_windows()

    assert windows == [{"window_id": "w1-1", "title": "Notepad"}]
    assert "handle" not in windows[0]
    assert "process_id" not in windows[0]


def test_desktop_inspect_binds_control_ids_to_the_selected_window() -> None:
    controller, _window, _button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]

    inspection = controller.desktop_inspect(window_id)
    controls = inspection["controls"]

    assert [item["name"] for item in controls] == ["Save", "Document"]
    assert all(item["control_id"].startswith("c1-") for item in controls)


def test_desktop_inspect_surfaces_named_tabs_separately_from_controls() -> None:
    controller, _window, _first_tab, _second_tab = _tabbed_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]

    inspection = controller.desktop_inspect(window_id)

    assert [(tab["name"], tab["active"]) for tab in inspection["tabs"]] == [
        ("API notes", True),
        ("Shopping list", False),
    ]
    assert [control["name"] for control in inspection["controls"]] == ["Text editor"]


def test_desktop_tab_verification_detects_a_switch_before_execution() -> None:
    controller, _window, first_tab, second_tab = _tabbed_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]
    inspection = controller.desktop_inspect(window_id)
    target = inspection["tabs"][1]
    controller.desktop_select(target["control_id"])
    first_tab.is_selected.return_value = False
    second_tab.is_selected.return_value = True
    controller.verify_active_tab(target)

    first_tab.is_selected.return_value = True
    second_tab.is_selected.return_value = False
    with pytest.raises(TabScopeError):
        controller.verify_active_tab(target)


def test_desktop_find_filters_only_inside_the_bound_window() -> None:
    controller, _window, _button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]
    controller.desktop_inspect(window_id)

    matches = controller.desktop_find(window_id, name="Save", control_type="Button")

    assert len(matches) == 1
    assert matches[0]["automation_id"] == "saveButton"


def test_desktop_invoke_uses_a_recent_scoped_control_id() -> None:
    controller, _window, button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]
    control_id = controller.desktop_inspect(window_id)["controls"][0]["control_id"]

    controller.desktop_invoke(control_id)

    button.invoke.assert_called_once_with()


def test_desktop_set_text_uses_uia_without_keystrokes() -> None:
    controller, window, _button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]
    control_id = controller.desktop_inspect(window_id)["controls"][1]["control_id"]
    edit = window.descendants.return_value[1]

    controller.desktop_set_text(control_id, "hello")

    edit.set_edit_text.assert_called_once_with("hello")


@pytest.mark.parametrize(("method", "uia_method"), [
    ("desktop_select", "select"),
    ("desktop_toggle", "toggle"),
])
def test_desktop_selection_actions_use_the_named_uia_control(method: str, uia_method: str) -> None:
    controller, _window, button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]
    control_id = controller.desktop_inspect(window_id)["controls"][0]["control_id"]

    getattr(controller, method)(control_id)

    getattr(button, uia_method).assert_called_once_with()


def test_desktop_scroll_translates_amount_to_bounded_uia_lines() -> None:
    controller, window, _button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]

    controller.desktop_scroll(window_id, "down", 3)

    window.scroll.assert_called_once_with(direction="down", amount="line", count=3)


def test_desktop_read_returns_named_control_text() -> None:
    controller, _window, button = _ready_controller()
    window_id = controller.desktop_list_windows()[0]["window_id"]
    control_id = controller.desktop_inspect(window_id)["controls"][0]["control_id"]

    assert controller.desktop_read(control_id) == "Save"
    button.window_text.assert_called()


def test_desktop_refs_expire_and_cannot_be_invented() -> None:
    now = [100.0]
    controller, window, _button = _ready_controller()
    controller._clock = lambda: now[0]
    window_id = controller.desktop_list_windows()[0]["window_id"]
    control_id = controller.desktop_inspect(window_id)["controls"][0]["control_id"]
    now[0] += 31.0

    with pytest.raises(InvalidDesktopReference):
        controller.desktop_invoke(control_id)
    with pytest.raises(InvalidDesktopReference):
        controller.desktop_invoke("c-invented")


def test_elevated_windows_are_refused_explicitly() -> None:
    controller, _window, _button = _ready_controller()
    controller._elevation_checker = lambda _pid: True
    window_id = controller.desktop_list_windows()[0]["window_id"]

    with pytest.raises(ElevatedWindowError):
        controller.desktop_inspect(window_id)


class _FakeDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.controls = {"c1-1": {"name": "Save", "control_type": "Button", "sensitive": False}}
        self.tabs: list[dict] = []
        self.verified_tabs: list[dict] = []

    def desktop_list_windows(self):
        return [{"window_id": "w1-1", "title": "Notepad"}]

    def desktop_inspect(self, window_id: str):
        self.calls.append(("desktop_inspect", {"window_id": window_id}))
        return {
            "controls": [{"control_id": control_id, **control} for control_id, control in self.controls.items()],
            "tabs": self.tabs,
        }

    def describe_control(self, control_id: str):
        if control_id not in self.controls:
            raise InvalidDesktopReference(control_id)
        return self.controls[control_id]

    def verify_active_tab(self, tab: dict) -> None:
        self.verified_tabs.append(tab)

    def dispatch(self, kind: str, args: dict):
        self.calls.append((kind, args))
        if kind == "desktop_select":
            for tab in self.tabs:
                tab["active"] = tab["control_id"] == args["control_id"]
        return {"ok": True}


def _resolver(*decisions):
    remaining = list(decisions)
    return lambda *_args, **_kwargs: remaining.pop(0)


def test_desktop_tool_confirms_setting_text_with_concrete_context(mock_config) -> None:
    desktop = _FakeDesktop()
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or True
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver(
            {"kind": "desktop_set_text", "args": {"control_id": "c1-1", "text": "hello"}, "risk": "consequential"},
            {"kind": "done", "args": {"summary": "Typed."}, "risk": "read_only"},
        ),
    )

    result = tool.run({"application": "Notepad", "task": "Type hello"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.success is True
    assert channel.requests == [("desktopInteract.desktop_set_text", {
        "application": "Notepad", "control": "Save", "control_type": "Button",
        "task": "Type hello", "text": "hello",
    })]
    assert ("desktop_set_text", {"control_id": "c1-1", "text": "hello"}) in desktop.calls


def test_desktop_tool_translates_a_uia_failure_into_a_clean_message(mock_config) -> None:
    desktop = _FakeDesktop()

    def failing_dispatch(kind: str, args: dict):
        desktop.calls.append((kind, args))
        if kind == "desktop_invoke":
            raise RuntimeError(
                "COMError(-2147220991, 'Der Vorgang ist aufgrund eines "
                "geänderten Zustands des Objekts nicht mehr gültig.', "
                "(None, None, None, 0, None))\n" + ("  at native call frame\n" * 40)
            )
        return {"ok": True}

    desktop.dispatch = failing_dispatch
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver({"kind": "desktop_invoke", "args": {"control_id": "c1-1"}, "risk": "ordinary"}),
    )

    result = tool.run({"application": "Notepad", "task": "Click Save"}, _ctx(mock_config))

    assert result.success is False
    assert result.error_code == ToolErrorCode.EXECUTION_FAILED.value
    assert result.error_message is not None
    assert len(result.error_message) < 200
    assert "COMError" not in result.error_message
    assert "native call frame" not in result.error_message


def _two_tab_desktop() -> _FakeDesktop:
    desktop = _FakeDesktop()
    desktop.controls = {
        "c1-1": {"name": "API notes", "control_type": "TabItem", "automation_id": "tab-1", "sensitive": False},
        "c1-2": {"name": "Shopping list", "control_type": "TabItem", "automation_id": "tab-2", "sensitive": False},
        "c1-3": {"name": "Text editor", "control_type": "Edit", "automation_id": "editor", "sensitive": False},
    }
    desktop.tabs = [
        {"control_id": "c1-1", "name": "API notes", "automation_id": "tab-1", "active": True},
        {"control_id": "c1-2", "name": "Shopping list", "automation_id": "tab-2", "active": False},
    ]
    return desktop


def test_desktop_tool_refuses_consequential_action_when_tab_target_is_ambiguous(mock_config) -> None:
    desktop = _two_tab_desktop()
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or True
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver({
            "kind": "desktop_set_text",
            "args": {"control_id": "c1-3", "text": "hello"},
            "risk": "consequential",
        }),
    )

    result = tool.run(
        {"application": "Notepad", "task": "Type hello into Notepad"},
        _ctx(replace(mock_config, security_level="critical")),
    )

    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert channel.requests == []
    assert not any(kind == "desktop_set_text" for kind, _args in desktop.calls)


def test_desktop_tool_selects_named_tab_and_includes_it_in_confirmation(mock_config) -> None:
    desktop = _two_tab_desktop()
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or True
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver(
            {"kind": "desktop_select", "args": {"control_id": "c1-2"}, "risk": "ordinary"},
            {"kind": "desktop_set_text", "args": {"control_id": "c1-3", "text": "milk"}, "risk": "consequential"},
            {"kind": "done", "args": {"summary": "Typed."}, "risk": "read_only"},
        ),
    )

    result = tool.run(
        {"application": "Notepad", "task": "Type milk into the Notepad tab called Shopping list"},
        _ctx(replace(mock_config, security_level="critical")),
    )

    assert result.success is True
    assert channel.requests[0][1]["tab"] == "Shopping list"
    assert desktop.verified_tabs[-1]["name"] == "Shopping list"
    assert ("desktop_set_text", {"control_id": "c1-3", "text": "milk"}) in desktop.calls


def test_desktop_tool_aborts_if_selected_tab_changes_before_execution(mock_config) -> None:
    desktop = _two_tab_desktop()
    desktop.verify_active_tab = MagicMock(side_effect=[None, TabScopeError("tab changed")])
    channel = SimpleNamespace(is_available=True, ask=lambda *_args: True)
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver(
            {"kind": "desktop_select", "args": {"control_id": "c1-2"}, "risk": "ordinary"},
            {"kind": "desktop_set_text", "args": {"control_id": "c1-3", "text": "milk"}, "risk": "consequential"},
        ),
    )

    result = tool.run(
        {"application": "Notepad", "task": "Type milk into the Notepad tab called Shopping list"},
        _ctx(replace(mock_config, security_level="critical")),
    )

    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert not any(kind == "desktop_set_text" for kind, _args in desktop.calls)


def test_desktop_tool_decline_stops_before_invocation(mock_config) -> None:
    desktop = _FakeDesktop()
    channel = SimpleNamespace(is_available=True, ask=lambda *_args: False)
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver({"kind": "desktop_invoke", "args": {"control_id": "c1-1"}, "risk": "consequential"}),
    )

    result = tool.run({"application": "Notepad", "task": "Save it"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert not any(kind == "desktop_invoke" for kind, _args in desktop.calls)


def test_desktop_tool_refuses_sensitive_text_without_confirmation(mock_config) -> None:
    desktop = _FakeDesktop()
    desktop.controls["c1-1"]["sensitive"] = True
    channel = SimpleNamespace(is_available=True, requests=[])
    channel.ask = lambda name, args: channel.requests.append((name, args)) or True
    SecurityGate(level="critical", channels={"desktop": channel}, confirm_channels=["desktop"])
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=_resolver({"kind": "desktop_set_text", "args": {"control_id": "c1-1", "text": "123456"}, "risk": "secret"}),
    )

    result = tool.run({"application": "Notepad", "task": "Enter the code"}, _ctx(replace(mock_config, security_level="critical")))

    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value
    assert channel.requests == []


def test_desktop_tool_enforces_its_action_cap(mock_config) -> None:
    desktop = _FakeDesktop()
    tool = DesktopInteractTool(
        controller=desktop,
        resolver=lambda *_args, **_kwargs: {
            "kind": "desktop_scroll",
            "args": {"target_id": "w1-1", "direction": "down", "amount": 1},
            "risk": "read_only",
        },
        max_actions=2,
    )

    result = tool.run({"application": "Notepad", "task": "Keep scrolling"}, _ctx(mock_config))

    assert result.error_code == ToolErrorCode.TIMEOUT.value
    assert [kind for kind, _args in desktop.calls].count("desktop_scroll") == 2


def test_desktop_tool_maps_elevated_window_to_permission_denied(mock_config) -> None:
    desktop = MagicMock()
    desktop.desktop_list_windows.side_effect = ElevatedWindowError("elevated")
    tool = DesktopInteractTool(controller=desktop, resolver=_resolver())

    result = tool.run({"application": "Admin", "task": "Click OK"}, _ctx(mock_config))

    assert result.error_code == ToolErrorCode.PERMISSION_DENIED.value


def test_desktop_action_validator_rejects_unknown_risk_and_forbidden_fields() -> None:
    assert _desktop_action({
        "kind": "desktop_invoke", "args": {"control_id": "c1-1"}, "risk": "unguarded",
    }, "w1-1") is None
    assert _desktop_action({
        "kind": "desktop_invoke", "args": {"control_id": "c1-1", "x": 20}, "risk": "ordinary",
    }, "w1-1") is None


def test_desktop_public_schema_is_narrow_and_excludes_starting_apps() -> None:
    tool = DesktopInteractTool(controller=_FakeDesktop(), resolver=_resolver())

    assert set(tool.inputSchema["properties"]) == {"application", "task"}
    assert set(tool.inputSchema["required"]) == {"application", "task"}
    assert "openOnComputer" in tool.description
    assert "coordinates" in tool.description
