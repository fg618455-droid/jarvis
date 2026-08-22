## desktopInteract Spec

### Purpose

Act on named controls in one already-running native Windows application through pywinauto's UI Automation backend. The tool covers buttons, menus, fields, lists, tabs, toggles, and scrollable controls that expose a semantic UIA tree.

### Contract

- **Name**: `desktopInteract`
- **Public input schema**: required `application` and `task` strings.
- **Registration**: present only while `computer_interaction_enabled` is true. The setting defaults to false.
- **Platform**: Windows only. `pywinauto==0.6.9` is pinned in both requirements files. The Windows launchers install `requirements.txt` and stop instead of starting Jarvis if dependency installation fails. pywinauto is imported lazily, and an unavailable package or platform returns `unsupported` without breaking daemon startup.
- **Loop**: at most eight actions per tool call. Reaching the cap returns `timeout`.

The internal UIA adapter exposes exactly:

```text
desktop_list_windows()
desktop_inspect(window_id)
desktop_find(window_id, name=None, control_type=None, automation_id=None)
desktop_invoke(control_id)
desktop_set_text(control_id, text)
desktop_select(control_id)
desktop_toggle(control_id)
desktop_scroll(window_id_or_control_id, direction, amount)
desktop_read(control_id)
```

`done` is a resolver outcome, not a UIA action.

### Window and control rules

- The application string is matched case-insensitively against titles returned by `Desktop(backend="uia").windows()`.
- Window refs are opaque `w<generation>-<index>` values. Public window records expose only the ref and title, never a native handle or process ID.
- Inspect binds the call to one window. It examines at most 300 descendants whose UIA process ID matches that window, retains no more than 60 named or actionable controls for the resolver, and filters nameless structural wrappers. Returned controls use opaque `c<generation>-<index>` refs with name, control type, automation ID, and whether UIA marks the field as a password.
- Named UIA `TabItem` descendants are returned separately in a `tabs` collection, with their opaque control ref, name, automation ID, and active state. At most 24 tabs are exposed. The containing `Tab` wrapper is structural noise and is not exposed as an ordinary control.
- Window and control refs expire after 30 seconds. Control actions validate the active window, expiry, and process binding on every use. Invented, stale, cross-process, and out-of-scope refs return `invalid_argument`.
- `desktop_find` filters only the active window's collected descendants. It cannot accept a raw handle or search the entire desktop after scope is established.
- Scroll accepts `up`, `down`, `left`, or `right` and one through ten UIA lines.
- A window with an elevated token may be listed, but inspection and action are refused with `permission_denied`. An unreadable process token is treated as elevated.

### Action resolution

The CHAT-tier resolver sees the user's task, the selected application window, up to 60 bounded controls, up to 24 tabs, and up to eight compact prior action records. A history record contains only the action kind, bounded arguments, and a short outcome of at most 300 characters. It never contains an earlier UIA inspection. Named controls are ranked by overlap with the task, and nameless structural nodes are removed before the payload reaches the model. The resolver must choose from the fixed action contract and classify the action as `read_only`, `ordinary`, `consequential`, or `secret`.

A deterministic validator replaces every resolver-supplied window reference with the currently scoped window, checks opaque control refs at execution, bounds strings and scroll amounts, and rejects coordinates, keystrokes, scripts, and command fields.

Malformed, empty, or contract-invalid resolver output triggers one repair call with the same bounded observation and an explicit JSON-only correction. A second invalid output fails closed. The repair and final failure are written to `debug_log`.

### Tab scoping

- A window with more than one named `TabItem` is treated as a multi-document window.
- The task must identify exactly one tab by its exposed name. Matching is normalised and language independent. If no unique named tab can be derived, selecting a tab or attempting a consequential action is refused and logged.
- Before invoking, setting text, selecting, or toggling any non-tab control in a multi-document window, the loop requires an explicit `desktop_select` of the identified tab during the same tool call. The currently focused tab alone is not sufficient.
- After selection, the loop inspects the window again and records the selected tab identity. The tab name is included in every later consequential confirmation.
- Immediately before a tab-scoped action is dispatched, the controller enumerates the current UIA tab items again and verifies that the selected tab is still uniquely active. A missing, duplicated, inactive, or changed tab aborts the action with `permission_denied` and a `debug_log` entry.

### Security

`desktopInteract` is a critical built-in, so its initial invocation uses the outer registry confirmation at the default security level. Consequential actions add their own `SecurityGate.confirm()` call:

- Listing, inspecting, finding, reading, and scrolling do not create an inner confirmation.
- Setting text is always confirmed with the application, concrete control, control type, task, and bounded text.
- Invoking, selecting, or toggling a control classified as consequential is confirmed with the concrete application and control.
- A UIA password field or resolver-classified password, one-time-code, API-key, authentication-token, or equivalent secret field is refused without offering confirmation.

The gate action name is `desktopInteract.<action>`. Important actions, confirmation requests and refusals, inspection counts, and cap exits are auditable through `debug_log` without logging secret values.

### What desktopInteract is NOT

- Not an application launcher. A request that only starts an application belongs to `openOnComputer`.
- Not coordinate clicking, mouse movement, raw keystrokes, global typing, or pixel automation.
- Not a shell, PowerShell, cmd, subprocess, or arbitrary executable surface.
- Not a screenshot or vision-model loop.
- Not suitable for games, canvases, inaccessible custom controls, or elevated applications.

### Testing

`tests/tools/builtin/test_desktop_interact.py` mocks the pywinauto/UIA boundary and checks all nine adapter methods, opaque IDs, window and process scoping, expiry, elevation refusal, exact UIA method calls, tab detection, tab ambiguity refusal, active-tab re-verification, tab-aware confirmation, per-action confirmation, and secret refusal. `tests/tools/builtin/test_interaction_resolver.py` uses a moderately complex Notepad fixture with 300 descendants, including 55 named controls and 20 tabs, to enforce an 18,000-character resolver payload ceiling. The tests do not require an open Windows application.

A manual test still needs Felix at the machine: start Notepad separately, enable computer interaction, then ask Jarvis to type text and save it. Verify named-control discovery, text confirmation, save confirmation, and the native Save dialog without using coordinates or keystrokes.
