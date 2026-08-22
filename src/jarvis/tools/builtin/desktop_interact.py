"""Windows UI Automation wrapper with expiring window-scoped references."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ...debug import debug_log
from ...security.gate import SecurityGate
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult
from .interaction_resolver import VALID_RISKS, resolve_semantic_action


DESKTOP_ACTIONS = frozenset({
    "desktop_list_windows",
    "desktop_inspect",
    "desktop_find",
    "desktop_invoke",
    "desktop_set_text",
    "desktop_select",
    "desktop_toggle",
    "desktop_scroll",
    "desktop_read",
})
_REF_TTL_SEC = 30.0
_MAX_CONTROLS = 300
_ACTION_CONTRACT = """
- desktop_list_windows {}
- desktop_inspect {window_id: ref supplied by the current observation}
- desktop_find {window_id: current window ref, name?: text, control_type?: text, automation_id?: text}
- desktop_invoke {control_id: ref supplied by inspect/find}
- desktop_set_text {control_id: ref supplied by inspect/find, text: text}
- desktop_select {control_id: ref supplied by inspect/find}
- desktop_toggle {control_id: ref supplied by inspect/find}
- desktop_scroll {target_id: current window/control ref, direction: up|down|left|right, amount: integer 1..10}
- desktop_read {control_id: ref supplied by inspect/find}
- done {summary: short user-facing outcome}
""".strip()


class DesktopInteractionError(RuntimeError):
    """Base error for the UIA adapter."""


class DesktopUnsupported(DesktopInteractionError):
    """UI Automation is unavailable on this platform or installation."""


class ElevatedWindowError(DesktopInteractionError):
    """The selected window crosses the normal/elevated process boundary."""


class InvalidDesktopReference(DesktopInteractionError):
    """A window/control ref is unknown, expired, or outside the active scope."""


@dataclass
class _WindowRef:
    wrapper: Any
    title: str
    process_id: int
    handle: int
    elevated: bool
    expires_at: float
    generation: int


@dataclass
class _ControlRef:
    wrapper: Any
    window_id: str
    name: str
    control_type: str
    automation_id: str
    process_id: int
    handle: int
    sensitive: bool
    expires_at: float
    generation: int


def _new_uia_desktop() -> Any:
    if sys.platform != "win32":
        raise DesktopUnsupported("desktopInteract is available only on Windows.")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise DesktopUnsupported("pywinauto is not installed.") from exc
    return Desktop(backend="uia")


def _is_process_elevated(process_id: int) -> bool:
    """Return whether a Windows process has an elevated token.

    An inaccessible token is treated as elevated. The safe failure is an
    explicit refusal rather than trying UIA across a privilege boundary.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(process_id))
        if not process:
            return True
        token = wintypes.HANDLE()
        try:
            if not ctypes.windll.advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
                return True
            elevation = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not ctypes.windll.advapi32.GetTokenInformation(
                token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(returned)
            ):
                return True
            return bool(elevation.value)
        finally:
            if token.value:
                ctypes.windll.kernel32.CloseHandle(token)
            ctypes.windll.kernel32.CloseHandle(process)
    except Exception:
        return True


def _element_value(wrapper: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(wrapper.element_info, name)
    except Exception:
        return default


class DesktopController:
    """Issue opaque refs for one UIA window subtree at a time."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._desktop_factory: Callable[[], Any] = _new_uia_desktop
        self._elevation_checker: Callable[[int], bool] = _is_process_elevated
        self._window_generation = 0
        self._control_generation = 0
        self._windows: dict[str, _WindowRef] = {}
        self._controls: dict[str, _ControlRef] = {}
        self._active_window_id: str | None = None

    def _get_window(self, window_id: object) -> _WindowRef:
        key = str(window_id or "").strip()
        record = self._windows.get(key)
        if record is None or record.expires_at < self._clock():
            raise InvalidDesktopReference("The window reference is stale or unknown.")
        if record.elevated:
            raise ElevatedWindowError("Jarvis will not automate an elevated window.")
        return record

    def _get_control(self, control_id: object) -> _ControlRef:
        key = str(control_id or "").strip()
        record = self._controls.get(key)
        if record is None or record.expires_at < self._clock():
            raise InvalidDesktopReference("The control reference is stale or unknown.")
        if record.window_id != self._active_window_id:
            raise InvalidDesktopReference("The control is outside the active window scope.")
        window = self._get_window(record.window_id)
        current_pid = int(_element_value(record.wrapper, "process_id", -1) or -1)
        if current_pid != window.process_id or record.process_id != window.process_id:
            raise InvalidDesktopReference("The control left the active window process.")
        return record

    def desktop_list_windows(self) -> list[dict[str, str]]:
        try:
            windows = self._desktop_factory().windows()
        except DesktopUnsupported:
            raise
        except Exception as exc:
            raise DesktopUnsupported("Windows UI Automation is unavailable.") from exc
        self._window_generation += 1
        self._windows = {}
        self._controls = {}
        self._active_window_id = None
        output: list[dict[str, str]] = []
        for wrapper in windows:
            try:
                title = str(wrapper.window_text() or _element_value(wrapper, "name", "")).strip()
                process_id = int(_element_value(wrapper, "process_id", 0) or 0)
                handle = int(_element_value(wrapper, "handle", 0) or 0)
            except Exception:
                continue
            if not title or not process_id:
                continue
            window_id = f"w{self._window_generation}-{len(output) + 1}"
            self._windows[window_id] = _WindowRef(
                wrapper=wrapper,
                title=title[:300],
                process_id=process_id,
                handle=handle,
                elevated=bool(self._elevation_checker(process_id)),
                expires_at=self._clock() + _REF_TTL_SEC,
                generation=self._window_generation,
            )
            output.append({"window_id": window_id, "title": title[:300]})
        debug_log(f"desktopInteract listed windows={len(output)}", "tools")
        return output

    def _control_record(self, window_id: str, wrapper: Any) -> tuple[str, _ControlRef] | None:
        window = self._get_window(window_id)
        process_id = int(_element_value(wrapper, "process_id", -1) or -1)
        if process_id != window.process_id:
            return None
        name = str(_element_value(wrapper, "name", "") or "").strip()
        if not name:
            try:
                name = str(wrapper.window_text() or "").strip()
            except Exception:
                name = ""
        control_type = str(_element_value(wrapper, "control_type", "") or "control")[:100]
        automation_id = str(_element_value(wrapper, "automation_id", "") or "")[:300]
        handle = int(_element_value(wrapper, "handle", 0) or 0)
        sensitive = bool(_element_value(wrapper, "is_password", False))
        control_id = f"c{self._control_generation}-{len(self._controls) + 1}"
        return control_id, _ControlRef(
            wrapper=wrapper,
            window_id=window_id,
            name=name[:300] or control_type,
            control_type=control_type,
            automation_id=automation_id,
            process_id=process_id,
            handle=handle,
            sensitive=sensitive,
            expires_at=self._clock() + _REF_TTL_SEC,
            generation=self._control_generation,
        )

    @staticmethod
    def _public_control(control_id: str, record: _ControlRef) -> dict[str, Any]:
        return {
            "control_id": control_id,
            "name": record.name,
            "control_type": record.control_type,
            "automation_id": record.automation_id,
            "sensitive": record.sensitive,
        }

    def desktop_inspect(self, window_id: str) -> list[dict[str, Any]]:
        window = self._get_window(window_id)
        self._active_window_id = window_id
        self._control_generation += 1
        self._controls = {}
        try:
            descendants = list(window.wrapper.descendants())[:_MAX_CONTROLS]
        except Exception as exc:
            raise DesktopInteractionError("The window controls could not be inspected.") from exc
        output: list[dict[str, Any]] = []
        for wrapper in descendants:
            result = self._control_record(window_id, wrapper)
            if result is None:
                continue
            control_id, record = result
            self._controls[control_id] = record
            output.append(self._public_control(control_id, record))
        debug_log(
            f"desktopInteract inspected window generation={self._control_generation} controls={len(output)}",
            "tools",
        )
        return output

    def desktop_find(
        self,
        window_id: str,
        name: str | None = None,
        control_type: str | None = None,
        automation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._get_window(window_id)
        if self._active_window_id != window_id or not self._controls:
            self.desktop_inspect(window_id)
        wanted_name = str(name).casefold() if name is not None else None
        wanted_type = str(control_type).casefold() if control_type is not None else None
        wanted_id = str(automation_id).casefold() if automation_id is not None else None
        matches: list[dict[str, Any]] = []
        for control_id, record in self._controls.items():
            if wanted_name is not None and record.name.casefold() != wanted_name:
                continue
            if wanted_type is not None and record.control_type.casefold() != wanted_type:
                continue
            if wanted_id is not None and record.automation_id.casefold() != wanted_id:
                continue
            matches.append(self._public_control(control_id, record))
        return matches

    def describe_control(self, control_id: str) -> dict[str, Any]:
        record = self._get_control(control_id)
        return self._public_control(control_id, record)

    def desktop_invoke(self, control_id: str) -> dict[str, Any]:
        self._get_control(control_id).wrapper.invoke()
        return {"invoked": control_id}

    def desktop_set_text(self, control_id: str, text: str) -> dict[str, Any]:
        self._get_control(control_id).wrapper.set_edit_text(str(text))
        return {"updated": control_id}

    def desktop_select(self, control_id: str) -> dict[str, Any]:
        self._get_control(control_id).wrapper.select()
        return {"selected": control_id}

    def desktop_toggle(self, control_id: str) -> dict[str, Any]:
        self._get_control(control_id).wrapper.toggle()
        return {"toggled": control_id}

    def desktop_scroll(self, target_id: str, direction: str, amount: int) -> dict[str, Any]:
        target_key = str(target_id or "").strip()
        if target_key in self._windows:
            wrapper = self._get_window(target_key).wrapper
        else:
            wrapper = self._get_control(target_key).wrapper
        normalised = str(direction).casefold()
        if normalised not in {"up", "down", "left", "right"}:
            raise ValueError("Unsupported UIA scroll direction.")
        count = max(1, min(10, int(amount)))
        wrapper.scroll(direction=normalised, amount="line", count=count)
        return {"direction": normalised, "amount": count}

    def desktop_read(self, control_id: str) -> str:
        record = self._get_control(control_id)
        try:
            text = str(record.wrapper.window_text() or "").strip()
        except Exception:
            text = ""
        return text or record.name

    def dispatch(self, kind: str, args: dict[str, Any]) -> Any:
        if kind not in DESKTOP_ACTIONS:
            raise ValueError(f"Unknown desktop action: {kind}")
        if kind == "desktop_list_windows":
            return self.desktop_list_windows()
        if kind == "desktop_inspect":
            return self.desktop_inspect(args["window_id"])
        if kind == "desktop_find":
            return self.desktop_find(
                args["window_id"], args.get("name"), args.get("control_type"), args.get("automation_id")
            )
        if kind == "desktop_invoke":
            return self.desktop_invoke(args["control_id"])
        if kind == "desktop_set_text":
            return self.desktop_set_text(args["control_id"], args["text"])
        if kind == "desktop_select":
            return self.desktop_select(args["control_id"])
        if kind == "desktop_toggle":
            return self.desktop_toggle(args["control_id"])
        if kind == "desktop_scroll":
            return self.desktop_scroll(args["target_id"], args["direction"], args["amount"])
        return {"text": self.desktop_read(args["control_id"])}


def resolve_desktop_action(cfg, task: str, observation: Any, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    return resolve_semantic_action(
        cfg,
        surface="desktop",
        task=task,
        observation=observation,
        history=history,
        action_contract=_ACTION_CONTRACT,
    )


def _desktop_action(value: Any, window_id: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    args = value.get("args", {})
    risk = value.get("risk", "consequential")
    if risk not in VALID_RISKS:
        return None
    if kind == "done":
        summary = args.get("summary") if isinstance(args, dict) else None
        return {"kind": "done", "args": {"summary": str(summary or "").strip()[:1000]}, "risk": "read_only"}
    if kind not in DESKTOP_ACTIONS or not isinstance(args, dict):
        return None
    if any(key in args for key in ("x", "y", "coordinates", "keystrokes", "command", "script")):
        return None
    if kind in {"desktop_list_windows"}:
        clean: dict[str, Any] = {}
    elif kind == "desktop_inspect":
        clean = {"window_id": window_id}
    elif kind == "desktop_find":
        clean = {"window_id": window_id}
        for key in ("name", "control_type", "automation_id"):
            if args.get(key) is not None:
                clean[key] = str(args[key]).strip()[:300]
    elif kind in {"desktop_invoke", "desktop_select", "desktop_toggle", "desktop_read"}:
        clean = {"control_id": str(args.get("control_id") or "").strip()}
    elif kind == "desktop_set_text":
        clean = {
            "control_id": str(args.get("control_id") or "").strip(),
            "text": str(args.get("text") or "")[:20_000],
        }
    else:
        direction = str(args.get("direction") or "").casefold()
        if direction not in {"up", "down", "left", "right"}:
            return None
        try:
            amount = max(1, min(10, int(args.get("amount", 1))))
        except (TypeError, ValueError):
            return None
        clean = {
            "target_id": str(args.get("target_id") or window_id).strip(),
            "direction": direction,
            "amount": amount,
        }
    return {"kind": kind, "args": clean, "risk": risk}


class DesktopInteractTool(Tool):
    """Run a bounded semantic action loop in one already-running app."""

    def __init__(
        self,
        *,
        controller: DesktopController | None = None,
        resolver: Callable[..., dict[str, Any] | None] = resolve_desktop_action,
        max_actions: int = 8,
    ) -> None:
        self._controller = controller or DesktopController()
        self._resolver = resolver
        self._max_actions = max(1, min(10, int(max_actions)))

    @property
    def name(self) -> str:
        return "desktopInteract"

    @property
    def description(self) -> str:
        return (
            "Read and interact with named controls in one already-running native Windows application. "
            "Use openOnComputer instead when the request only starts or opens an application. This tool "
            "does not start apps and cannot operate games, canvases, screen coordinates, raw keystrokes, "
            "elevated windows, scripts, or commands."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "application": {
                    "type": "string",
                    "description": "The already-running application whose named window should be scoped.",
                },
                "task": {
                    "type": "string",
                    "description": "The complete task to perform through named UI Automation controls.",
                },
            },
            "required": ["application", "task"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        application = str((args or {}).get("application") or "").strip()
        task = str((args or {}).get("task") or "").strip()
        if not application or not task:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_ARGUMENT,
                "desktopInteract needs an already-running application and a task.",
                phase="routing",
            )
        try:
            windows = self._controller.desktop_list_windows()
            app_key = application.casefold()
            matches = [item for item in windows if app_key in str(item.get("title") or "").casefold()]
            if not matches:
                return ToolExecutionResult.failure(
                    ToolErrorCode.INVALID_ARGUMENT,
                    f"No running window matches {application!r}.",
                )
            window_id = str(matches[0]["window_id"])
            controls = self._controller.desktop_inspect(window_id)
        except DesktopUnsupported as exc:
            return ToolExecutionResult.failure(ToolErrorCode.UNSUPPORTED, str(exc), phase="preflight")
        except ElevatedWindowError as exc:
            return ToolExecutionResult.failure(ToolErrorCode.PERMISSION_DENIED, str(exc), phase="preflight")
        except DesktopInteractionError as exc:
            return ToolExecutionResult.failure(
                ToolErrorCode.EXECUTION_FAILED,
                "The application window could not be inspected.",
                technical_details=str(exc),
            )

        observation: Any = {"window": matches[0], "controls": controls}
        history: list[dict[str, Any]] = []
        gate = SecurityGate.get_or_create(context.cfg)
        context.user_print(f"🪟 Desktop interaction started in {application}.")

        for turn in range(self._max_actions):
            decision = _desktop_action(self._resolver(context.cfg, task, observation, history), window_id)
            if decision is None:
                return ToolExecutionResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    "The desktop action resolver returned no safe action.",
                    technical_details="invalid resolver action",
                )
            kind, action_args, risk = decision["kind"], decision["args"], decision["risk"]
            if kind == "done":
                summary = action_args.get("summary") or "Desktop interaction completed."
                return ToolExecutionResult(success=True, reply_text=summary)

            control: dict[str, Any] = {}
            control_id = action_args.get("control_id")
            target_id = action_args.get("target_id")
            if control_id or (target_id and str(target_id).startswith("c")):
                try:
                    control = self._controller.describe_control(str(control_id or target_id))
                except InvalidDesktopReference as exc:
                    return ToolExecutionResult.failure(
                        ToolErrorCode.INVALID_ARGUMENT,
                        "The desktop control reference is stale or outside the selected window.",
                        technical_details=str(exc),
                    )
            if risk == "secret" or (kind == "desktop_set_text" and control.get("sensitive")):
                debug_log(f"desktopInteract refused sensitive action kind={kind}", "security")
                return ToolExecutionResult.failure(
                    ToolErrorCode.PERMISSION_DENIED,
                    "Jarvis will not enter passwords, one-time codes, API keys, or authentication secrets.",
                    phase="confirmation",
                )
            consequential = (
                kind == "desktop_set_text"
                or (kind in {"desktop_invoke", "desktop_select", "desktop_toggle"} and risk == "consequential")
            )
            if consequential:
                confirmation = {
                    "application": application,
                    "control": str(control.get("name") or kind)[:300],
                    "control_type": str(control.get("control_type") or "")[:100],
                    "task": task[:1000],
                }
                if kind == "desktop_set_text":
                    confirmation["text"] = action_args["text"][:500]
                debug_log(f"desktopInteract requesting confirmation kind={kind}", "security")
                if not gate.confirm(f"desktopInteract.{kind}", confirmation):
                    debug_log(f"desktopInteract confirmation denied kind={kind}", "security")
                    return ToolExecutionResult.failure(
                        ToolErrorCode.PERMISSION_DENIED,
                        "The desktop action was declined.",
                        phase="confirmation",
                    )
            debug_log(f"desktopInteract action {turn + 1}/{self._max_actions}: {kind}", "tools")
            try:
                result = self._controller.dispatch(kind, action_args)
                if kind in {"desktop_invoke", "desktop_set_text", "desktop_select", "desktop_toggle"}:
                    controls = self._controller.desktop_inspect(window_id)
                    observation = {"window": matches[0], "controls": controls}
                else:
                    observation = result
            except ElevatedWindowError as exc:
                return ToolExecutionResult.failure(ToolErrorCode.PERMISSION_DENIED, str(exc))
            except InvalidDesktopReference as exc:
                return ToolExecutionResult.failure(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "The desktop control reference is stale or outside the selected window.",
                    technical_details=str(exc),
                )
            except (DesktopInteractionError, ValueError, KeyError) as exc:
                return ToolExecutionResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    "The desktop action could not be completed.",
                    technical_details=f"{type(exc).__name__}: {exc}",
                )
            history.append({"kind": kind, "args": action_args, "observation": observation})

        debug_log(f"desktopInteract hit action cap={self._max_actions}", "security")
        return ToolExecutionResult.failure(
            ToolErrorCode.TIMEOUT,
            "The desktop interaction stopped at its action limit.",
        )
