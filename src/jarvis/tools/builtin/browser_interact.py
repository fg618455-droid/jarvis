"""Headed, isolated Playwright automation through snapshot-scoped refs."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin, urlparse

from ...debug import debug_log
from ...security.gate import SecurityGate
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult
from .interaction_resolver import VALID_RISKS, resolve_semantic_action


BROWSER_ACTIONS = frozenset({
    "browser_open",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
    "browser_scroll",
    "browser_read",
    "browser_back",
    "browser_close",
})
_ACTIONABLE_SELECTOR = (
    "a, button, input, textarea, select, [role='button'], [role='link'], "
    "[role='textbox'], [role='checkbox'], [role='radio'], [role='combobox'], "
    "[role='tab'], [role='menuitem'], [role='option']"
)
_MAX_CONTROLS = 200
_SCROLL_PIXELS_PER_UNIT = 600
_ACTION_CONTRACT = """
- browser_open {url: http(s) URL}
- browser_snapshot {}
- browser_click {ref: ref from the latest snapshot}
- browser_fill {ref: ref from the latest snapshot, text: text}
- browser_scroll {direction: up|down, amount: integer 1..10}
- browser_read {ref?: ref from the latest snapshot}
- browser_back {}
- browser_close {}
- done {summary: short user-facing outcome}
""".strip()


class BrowserInteractionError(RuntimeError):
    """Base error for the narrow Playwright adapter."""


class BrowserUnsupported(BrowserInteractionError):
    """Playwright or its installed browser is unavailable."""


class BrowserUnavailable(BrowserInteractionError):
    """No Jarvis-owned browser page exists yet."""


class InvalidBrowserReference(BrowserInteractionError):
    """A ref is unknown or no longer belongs to the latest snapshot."""


@dataclass
class _BrowserRef:
    locator: Any
    name: str
    role: str
    href: str | None
    domain: str
    sensitive: bool
    generation: int


def _new_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnsupported("Playwright is not installed.") from exc
    return sync_playwright().start()


def _http_url(url: object) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Only http and https addresses are allowed.")
    return value


class BrowserController:
    """Own one headed browser with an isolated, non-persistent context."""

    def __init__(self) -> None:
        self._playwright_factory: Callable[[], Any] = _new_playwright
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._generation = 0
        self._refs: dict[str, _BrowserRef] = {}

    def _start(self) -> None:
        if self._page is not None:
            return
        try:
            self._playwright = self._playwright_factory()
            # launch() plus a fresh browser context is isolated from the
            # user's Chrome/Edge profiles, cookies, passwords, and sessions.
            self._browser = self._playwright.chromium.launch(headless=False)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        except BrowserUnsupported:
            raise
        except Exception as exc:
            self.browser_close()
            raise BrowserUnsupported(
                "The pinned Playwright browser is not installed or could not start."
            ) from exc

    def _require_page(self) -> Any:
        if self._page is None:
            raise BrowserUnavailable("No Jarvis browser page is open.")
        return self._page

    def _get_ref(self, ref: object) -> _BrowserRef:
        key = str(ref or "").strip()
        record = self._refs.get(key)
        if record is None or record.generation != self._generation:
            raise InvalidBrowserReference(
                "The browser ref is stale or unknown. Take a new snapshot."
            )
        return record

    def describe_ref(self, ref: str) -> dict[str, Any]:
        record = self._get_ref(ref)
        return {
            "name": record.name,
            "role": record.role,
            "href": record.href,
            "domain": record.domain,
            "sensitive": record.sensitive,
        }

    def browser_open(self, url: str) -> dict[str, Any]:
        target = _http_url(url)
        self._start()
        page = self._require_page()
        page.goto(target, wait_until="domcontentloaded")
        self._refs.clear()
        debug_log(f"browserInteract opened domain={urlparse(target).hostname or ''}", "tools")
        return {"url": target}

    def browser_snapshot(self) -> dict[str, Any]:
        page = self._require_page()
        body = page.locator("body")
        try:
            text = (body.inner_text() or "").strip()
        except Exception:
            text = ""
        try:
            tree = (body.aria_snapshot() or "").strip()
        except (AttributeError, TypeError):
            tree = text

        self._generation += 1
        self._refs = {}
        controls: list[dict[str, Any]] = []
        candidates = page.locator(_ACTIONABLE_SELECTOR)
        try:
            count = min(int(candidates.count()), _MAX_CONTROLS)
        except Exception:
            count = 0
        for index in range(count):
            locator = candidates.nth(index)
            try:
                if not locator.is_visible():
                    continue
                attributes = {
                    name: locator.get_attribute(name)
                    for name in ("aria-label", "placeholder", "title", "role", "href", "type", "autocomplete")
                }
                inner = (locator.inner_text() or "").strip()
            except Exception:
                continue
            name = str(
                attributes.get("aria-label")
                or attributes.get("placeholder")
                or attributes.get("title")
                or inner
                or attributes.get("role")
                or attributes.get("type")
                or "control"
            ).strip()[:300]
            role = str(attributes.get("role") or attributes.get("type") or "control")[:80]
            raw_href = attributes.get("href")
            href = urljoin(str(page.url), str(raw_href)) if raw_href else None
            domain = (urlparse(href or str(page.url)).hostname or "").casefold()
            input_type = str(attributes.get("type") or "").casefold()
            autocomplete = str(attributes.get("autocomplete") or "").casefold()
            sensitive = input_type == "password" or autocomplete in {
                "current-password", "new-password", "one-time-code",
            }
            ref = f"b{self._generation}-{len(controls) + 1}"
            self._refs[ref] = _BrowserRef(
                locator=locator,
                name=name,
                role=role,
                href=href,
                domain=domain,
                sensitive=sensitive,
                generation=self._generation,
            )
            controls.append({
                "ref": ref,
                "name": name,
                "role": role,
                "href": href,
                "sensitive": sensitive,
            })
        snapshot = {
            "url": str(page.url),
            "accessibility_tree": tree[:30_000],
            "text": text[:30_000],
            "controls": controls,
        }
        debug_log(
            f"browserInteract snapshot generation={self._generation} controls={len(controls)}",
            "tools",
        )
        return snapshot

    def browser_click(self, ref: str) -> dict[str, Any]:
        self._get_ref(ref).locator.click()
        return {"url": str(self._require_page().url)}

    def browser_fill(self, ref: str, text: str) -> dict[str, Any]:
        self._get_ref(ref).locator.fill(str(text))
        return {"filled": ref}

    def browser_scroll(self, direction: str, amount: int) -> dict[str, Any]:
        page = self._require_page()
        normalised = str(direction).casefold()
        if normalised not in {"up", "down"}:
            raise ValueError("Scroll direction must be up or down.")
        units = max(1, min(10, int(amount)))
        delta = units * _SCROLL_PIXELS_PER_UNIT * (1 if normalised == "down" else -1)
        page.mouse.wheel(0, delta)
        return {"direction": normalised, "amount": units}

    def browser_read(self, ref: str | None = None) -> str:
        if ref is None:
            return (self._require_page().locator("body").inner_text() or "").strip()
        record = self._get_ref(ref)
        try:
            return (record.locator.inner_text() or "").strip()
        except Exception:
            try:
                return (record.locator.input_value() or "").strip()
            except Exception:
                return record.name

    def browser_back(self) -> dict[str, Any]:
        self._require_page().go_back(wait_until="domcontentloaded")
        self._refs.clear()
        return {"url": str(self._require_page().url)}

    def browser_close(self) -> dict[str, Any]:
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._context = self._browser = self._playwright = self._page = None
        self._refs.clear()
        return {"closed": True}

    def dispatch(self, kind: str, args: dict[str, Any]) -> Any:
        if kind not in BROWSER_ACTIONS:
            raise ValueError(f"Unknown browser action: {kind}")
        if kind == "browser_open":
            return self.browser_open(args["url"])
        if kind == "browser_snapshot":
            return self.browser_snapshot()
        if kind == "browser_click":
            return self.browser_click(args["ref"])
        if kind == "browser_fill":
            return self.browser_fill(args["ref"], args["text"])
        if kind == "browser_scroll":
            return self.browser_scroll(args["direction"], args["amount"])
        if kind == "browser_read":
            return {"text": self.browser_read(args.get("ref"))}
        if kind == "browser_back":
            return self.browser_back()
        return self.browser_close()


def resolve_browser_action(cfg, task: str, observation: Any, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    return resolve_semantic_action(
        cfg,
        surface="browser",
        task=task,
        observation=observation,
        history=history,
        action_contract=_ACTION_CONTRACT,
    )


def _browser_action(value: Any) -> dict[str, Any] | None:
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
    if kind not in BROWSER_ACTIONS or not isinstance(args, dict):
        return None
    try:
        if kind == "browser_open":
            clean = {"url": _http_url(args.get("url"))}
        elif kind in {"browser_click"}:
            clean = {"ref": str(args.get("ref") or "").strip()}
        elif kind == "browser_fill":
            clean = {
                "ref": str(args.get("ref") or "").strip(),
                "text": str(args.get("text") or "")[:20_000],
            }
        elif kind == "browser_scroll":
            direction = str(args.get("direction") or "").casefold()
            if direction not in {"up", "down"}:
                return None
            clean = {"direction": direction, "amount": max(1, min(10, int(args.get("amount", 1))))}
        elif kind == "browser_read":
            clean = {}
            if args.get("ref") is not None:
                clean["ref"] = str(args["ref"]).strip()
        else:
            clean = {}
    except (TypeError, ValueError):
        return None
    if any(key in args for key in ("selector", "x", "y", "javascript", "script", "command")):
        return None
    return {"kind": kind, "args": clean, "risk": risk}


def _task_names_domain(task: str, domain: str) -> bool:
    """Match technical host labels against the task without language lists."""
    task_key = re.sub(r"[^a-z0-9]", "", task.casefold())
    labels = [
        re.sub(r"[^a-z0-9]", "", label.casefold())
        for label in domain.split(".")
    ]
    return any(len(label) >= 3 and label in task_key for label in labels)


class BrowserInteractTool(Tool):
    """Run a bounded semantic action loop in Jarvis's isolated browser."""

    def __init__(
        self,
        *,
        controller: BrowserController | None = None,
        resolver: Callable[..., dict[str, Any] | None] = resolve_browser_action,
        max_actions: int = 8,
    ) -> None:
        self._controller = controller or BrowserController()
        self._resolver = resolver
        self._max_actions = max(1, min(10, int(max_actions)))

    @property
    def name(self) -> str:
        return "browserInteract"

    def close(self) -> None:
        """Release a Jarvis-owned browser when the capability is disabled."""
        self._controller.browser_close()

    @property
    def description(self) -> str:
        return (
            "Read and interact with web pages through named controls in Jarvis's isolated browser. "
            "Use this tool for opening and interacting in one request because it owns navigation. "
            "If the request only opens or shows something, use openOnComputer instead. Never use "
            "this for native applications, coordinates, scripts, or the user's signed-in browser."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The complete browser task, including any site the user named.",
                }
            },
            "required": ["task"],
        }

    def _snapshot_or_empty(self) -> dict[str, Any]:
        try:
            return self._controller.dispatch("browser_snapshot", {})
        except BrowserUnavailable:
            return {"state": "no Jarvis browser page is open"}

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        task = str((args or {}).get("task") or "").strip()
        if not task:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_ARGUMENT,
                "browserInteract needs a browser task.",
                phase="routing",
            )
        history: list[dict[str, Any]] = []
        try:
            observation: Any = self._snapshot_or_empty()
        except BrowserUnsupported as exc:
            return ToolExecutionResult.failure(ToolErrorCode.UNSUPPORTED, str(exc), phase="preflight")
        origin_domain = (urlparse(str(observation.get("url") or "")).hostname or "").casefold()
        gate = SecurityGate.get_or_create(context.cfg)
        context.user_print("🌐 Browser interaction started.")

        for turn in range(self._max_actions):
            decision = _browser_action(self._resolver(context.cfg, task, observation, history))
            if decision is None:
                return ToolExecutionResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    "The browser action resolver returned no safe action.",
                    technical_details="invalid resolver action",
                )
            kind, action_args, risk = decision["kind"], decision["args"], decision["risk"]
            if kind == "done":
                summary = action_args.get("summary") or "Browser interaction completed."
                return ToolExecutionResult(success=True, reply_text=summary)

            ref_info: dict[str, Any] = {}
            if "ref" in action_args:
                try:
                    ref_info = self._controller.describe_ref(action_args["ref"])
                except InvalidBrowserReference as exc:
                    return ToolExecutionResult.failure(
                        ToolErrorCode.INVALID_ARGUMENT,
                        "The browser reference is stale or unknown. Take a new snapshot.",
                        technical_details=str(exc),
                    )
            if risk == "secret" or (kind == "browser_fill" and ref_info.get("sensitive")):
                debug_log(f"browserInteract refused sensitive action kind={kind}", "security")
                return ToolExecutionResult.failure(
                    ToolErrorCode.PERMISSION_DENIED,
                    "Jarvis will not enter passwords, one-time codes, API keys, or authentication secrets.",
                    phase="confirmation",
                )

            destination = ""
            if kind == "browser_open":
                destination = (urlparse(action_args["url"]).hostname or "").casefold()
            elif kind == "browser_click":
                destination = str(ref_info.get("domain") or "").casefold()
            crosses_domain = bool(origin_domain and destination and destination != origin_domain)
            unnamed_navigation = bool(
                kind == "browser_open"
                and destination
                and not _task_names_domain(task, destination)
            )
            consequential = (
                kind == "browser_fill"
                or risk == "consequential"
                or crosses_domain
                or unnamed_navigation
            )
            if consequential:
                confirmation = {
                    "control": str(ref_info.get("name") or action_args.get("url") or kind)[:300],
                    "domain": destination or origin_domain,
                    "task": task[:1000],
                }
                if kind == "browser_fill":
                    confirmation["text"] = action_args["text"][:500]
                debug_log(f"browserInteract requesting confirmation kind={kind}", "security")
                if not gate.confirm(f"browserInteract.{kind}", confirmation):
                    debug_log(f"browserInteract confirmation denied kind={kind}", "security")
                    return ToolExecutionResult.failure(
                        ToolErrorCode.PERMISSION_DENIED,
                        "The browser action was declined.",
                        phase="confirmation",
                    )
            debug_log(f"browserInteract action {turn + 1}/{self._max_actions}: {kind}", "tools")
            try:
                result = self._controller.dispatch(kind, action_args)
                if kind == "browser_open" and not origin_domain:
                    origin_domain = destination
                if kind in {"browser_open", "browser_click", "browser_fill", "browser_scroll", "browser_back"}:
                    observation = self._snapshot_or_empty()
                else:
                    observation = result
            except InvalidBrowserReference as exc:
                return ToolExecutionResult.failure(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "The browser reference is stale or unknown. Take a new snapshot.",
                    technical_details=str(exc),
                )
            except BrowserUnsupported as exc:
                return ToolExecutionResult.failure(ToolErrorCode.UNSUPPORTED, str(exc), phase="preflight")
            except (BrowserInteractionError, ValueError, KeyError) as exc:
                return ToolExecutionResult.failure(
                    ToolErrorCode.EXECUTION_FAILED,
                    "The browser action could not be completed.",
                    technical_details=f"{type(exc).__name__}: {exc}",
                )
            history.append({"kind": kind, "args": action_args, "observation": observation})

        debug_log(f"browserInteract hit action cap={self._max_actions}", "security")
        return ToolExecutionResult.failure(
            ToolErrorCode.TIMEOUT,
            "The browser interaction stopped at its action limit.",
            phase="execution",
        )
