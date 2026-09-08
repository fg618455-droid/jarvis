# Tool Runtime Specification

Every tool returns `ToolExecutionResult`. Failures carry a stable `ToolErrorCode`, a user-safe message, an execution phase, retryability, optional technical metadata, and a correlation ID. Raw exception text and credentials are not placed in the user-facing message.

`fetchWebPage` accepts HTTP and HTTPS only. It resolves every hostname before connecting and rejects loopback, private, link-local, multicast, reserved, unspecified, and metadata-service addresses. Redirects are followed manually and every destination is validated again. Responses are streamed with byte and text limits.

`screenshot` selects a platform capture adapter, stores the image in a temporary directory, and returns OCR text only. Missing capture or OCR dependencies, permission denial, timeout, capture failure, and empty OCR results are explicit failures rather than successful empty output.

On Windows, screenshot preflight accepts `TESSERACT_CMD`, PATH, or the standard
Program Files Tesseract location and configures `pytesseract` with the resolved
executable. The live acceptance must recognise text from an actual capture,
not merely import the dependencies.

`localFiles` resolves `~/...` and bare relative paths beneath the user's home
directory. Resolution and traversal checks still reject escapes from that
sandbox. Test doubles for optional BeautifulSoup import patch only `bs4`; they
must not replace Python's global import mechanism and accidentally disable
logging or redaction dependencies.

MCP npm catalogue entries use exact versions. Preflight reads the package specifier from the first non-flag argument of an `npx` command; every later argument belongs to the server being launched, so a server that takes a URL or a path is not mistaken for an unpinned package. A scoped name carries a leading `@` that is part of the name, so the version separator is the first `@` after the scope: `@scope/server` is unpinned and `@scope/server@1.2.3` is pinned. Tool errors retain stable codes across built-in and MCP execution paths.

The curated pins accepted on 2026-09-03 are
`chrome-devtools-mcp@1.8.0` and
`@coyasong/youtube-mcp-server@1.2.0`; each passed cold discovery and a harmless
real tool call. The retired YouTube 0.1.1 package is not offered. Rube's former
endpoint is retired; its successor `https://connect.composio.dev/mcp` requires
a fresh interactive Composio OAuth login and is not treated as accepted until
that completes.

`memoryProvenance` is a read-only built-in for questions about the source of a
remembered fact. It receives locally carried `RetrievedSnippet` records through
`ToolContext` and returns raw JSON, never a composed answer. With no carried
source it returns `status: not_recorded`; the reply prompt forbids inventing a
date, graph node, note title, or vault path. Vault paths are emitted only when
they are safe vault-relative identifiers. The tool's semantic description,
not a phrase matcher, tells the ordinary router when to select it.
When sourced retrieval coexists with warm-profile or hot-window context, the
tool reports `status: partial`; the model cites a record only when its snippet
supports the questioned fact.

`getExamCountdown` is a read-only School-memory tool. It returns an
`as_of_date` and raw examination records containing subject, the exact stored
date text, and a nullable local-day countdown. It never composes an answer or
chooses urgency wording. Date normalisation is conservative: an extractor's
ISO candidate is accepted only when the stored date text supplies explicit
day and year evidence; all uncertain dates keep `days_remaining: null`.

The opt-in live acceptance harness requires an explicit BOM-free JSON configuration and an existing NAS/Synology workspace before any writes. OCR acceptance displays known text on the primary screen and invokes screenshot through the registry. Safe reports retain only status and token-match booleans. Other tool checks remain separate acceptance gates.

The guarded harness also supports supervised temporary-file and nutrition-database write/read/delete checks, fail-closed registry checks, actual-vault read-only search, disposable-process MCP refresh, and subscription text-parser/dispatcher acceptance. It never uses the productive nutrition database. Supervised approval and denial remain separate outcomes; absence of a channel proves fail-closed policy, not an explicit user denial.
