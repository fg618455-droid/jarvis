## browserInteract Spec

### Purpose

Act inside web pages through Playwright's semantic page surface. The tool is for requests that need reading, navigation, clicking, filling, or scrolling, including requests that combine opening a site with interacting in it. Jarvis owns that navigation so the action loop retains a handle to the page.

### Contract

- **Name**: `browserInteract`
- **Public input schema**: one required `task` string.
- **Registration**: present only while `computer_interaction_enabled` is true. The setting defaults to false.
- **Lifecycle**: repeated enabled configuration reuses the live controller. Disabling the capability closes any Jarvis-owned browser before unregistering the tools.
- **Loop**: at most eight actions per tool call. The loop observes the current page, resolves one action, validates it, confirms it when required, executes it, and observes again. Reaching the cap returns `timeout`.
- **Output**: a factual completion summary after a grounded read, or the fixed `Browser interaction completed.` message after action-only completion. Missing or unsafe actions return a stable `ToolExecutionResult` failure rather than falling through to a broader mechanism.

The internal Playwright adapter exposes exactly:

```text
browser_open(url)
browser_snapshot()
browser_click(ref)
browser_fill(ref, text)
browser_scroll(direction, amount)
browser_read(ref=None)
browser_back()
browser_close()
```

`done` is a resolver outcome, not a browser action.

### Browser and reference rules

- Chromium is headed. `playwright==1.62.0` pins the Python package and its matching browser revision; the platform launch scripts run `python -m playwright install chromium` through that pinned package.
- Each controller launches a fresh browser context. It never attaches to Chrome or Edge and never reads the user's cookies, saved passwords, or logged-in sessions.
- Only `http` and `https` URLs are accepted. `file`, `javascript`, `data`, and every other scheme are refused before Playwright starts.
- A snapshot contains at most 6,000 characters each of page text and accessibility tree, plus at most 40 visible semantic controls. Nameless `group`, `region`, `generic`, and `pane` wrapper lines are removed from the tree.
- Every control receives an opaque `b<generation>-<index>` ref. A new snapshot invalidates every earlier ref. The model never supplies a CSS selector or DOM query.
- Click and fill resolve only through the ref table created by the most recent snapshot. Unknown and stale refs return `invalid_argument` and tell the caller to snapshot again.
- Scroll accepts only `up` or `down` and a bounded amount from one through ten. The fixed internal wheel delta is not part of the model-facing schema.

### Action resolution

The CHAT-tier resolver sees the user's task, up to eight compact prior action records, and the current bounded snapshot. A history record contains only the action kind, bounded arguments, and a short outcome of at most 300 characters. It never contains a prior snapshot. The resolver payload further limits page text and accessibility tree to 3,000 characters each and ranks at most 32 named controls by overlap with the task while retaining document order among the selected controls. Snapshot content is marked untrusted. The resolver must select one item from the fixed action contract and classify the action as `read_only`, `ordinary`, `consequential`, or `secret`.

Resolver output receives a second deterministic validation pass. Unknown action kinds, unexpected argument shapes, selectors, coordinates, scripts, JavaScript, and command fields are rejected. Malformed, empty, or contract-invalid output triggers one repair call with the same bounded observation and an explicit JSON-only correction. A second invalid output fails closed for the tool call. The repair and final failure are written to `debug_log`.

### Grounded completion

- A `done` with a non-empty summary is a factual UI answer. It is accepted only when the immediately preceding executed action was `browser_read`.
- A snapshot locates and bounds page content but does not ground a factual completion summary. `browser_snapshot` followed by a factual `done` is rejected.
- Rejection consumes the current slot in the existing eight-action budget. The next resolver turn receives explicit feedback that it must read the actual UI state before answering.
- An action-only task finishes with `done {}`. The tool supplies its fixed completion message, so clicking, filling, scrolling, navigation, and closing do not require an unrelated read.
- Accepted read-grounded completions, accepted action-only completions, and rejected ungrounded completions are recorded in `debug_log`.

### Security

`browserInteract` is a critical built-in, so the outer registry gate confirms its first invocation at the default security level. The loop adds a separate confirmation for every consequential action through `SecurityGate.confirm()`:

- Snapshot, read, scroll, back, and close do not create an inner confirmation.
- Filling any ordinary field is confirmed with the concrete control, domain, task, and bounded text.
- A click classified as consequential is confirmed with the concrete control and domain.
- Navigating or following a link from the task's starting domain to another domain is confirmed.
- A password, one-time-code, API-key, authentication-token, or equivalent secret field is refused outright. It is not offered for confirmation.

The gate action name is `browserInteract.<action>`. The arguments remain structured data so desktop, web, Telegram, and voice channels render them through the existing confirmation conventions. Important actions, confirmation requests and refusals, snapshots, and loop-cap exits are sent to `debug_log` without logging secret values.

A Playwright failure inside the action loop (a timeout, a locator resolving to nothing, an intercepted click) is caught and reduced to its first line before it reaches `ToolExecutionResult`. Playwright's own error text carries a full retry call log, which is valuable in `technical_details` and unusable as a user-facing message.

### What browserInteract is NOT

- Not a way to run JavaScript or evaluate code in the page.
- Not a raw Playwright or Playwright MCP pass-through.
- Not a shell, command-line, subprocess, or file-navigation surface.
- Not coordinate clicking, raw typing, screenshots, OCR, or a vision-model loop.
- Not an attachment to the user's normal browser profile.
- Not the right tool for a request that only opens something. That remains `openOnComputer`.

### Testing

`tests/tools/builtin/test_browser_interact.py` mocks the Playwright boundary and checks all eight adapter methods, headed isolated startup, URL-scheme refusal, snapshot generations, stale refs, exact clicks and fills, scrolling, reading, back, close, compact action history, grounded and rejected completion, per-action confirmation, secret refusal, and the action cap. `tests/tools/builtin/test_interaction_resolver.py` uses a DuckDuckGo-scale fixture with 200 controls and large text and tree fields to enforce an 18,000-character resolver payload ceiling, retained grounding feedback, and the one-shot JSON repair. The tests never require a real browser.

A manual test still needs Felix at the machine: enable computer interaction and ask Jarvis to open YouTube and play the second result. Verify that a visible isolated Chromium window opens, ordinary in-domain navigation works, and consequential or cross-domain actions show concrete confirmations.
