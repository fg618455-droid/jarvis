# Tool Runtime Specification

Every tool returns `ToolExecutionResult`. Failures carry a stable `ToolErrorCode`, a user-safe message, an execution phase, retryability, optional technical metadata, and a correlation ID. Raw exception text and credentials are not placed in the user-facing message.

`fetchWebPage` accepts HTTP and HTTPS only. It resolves every hostname before connecting and rejects loopback, private, link-local, multicast, reserved, unspecified, and metadata-service addresses. Redirects are followed manually and every destination is validated again. Responses are streamed with byte and text limits.

`screenshot` selects a platform capture adapter, stores the image in a temporary directory, and returns OCR text only. Missing capture or OCR dependencies, permission denial, timeout, capture failure, and empty OCR results are explicit failures rather than successful empty output.

MCP npm catalogue entries use exact versions. Preflight reads the package specifier from the first non-flag argument of an `npx` command; every later argument belongs to the server being launched, so a server that takes a URL or a path is not mistaken for an unpinned package. A scoped name carries a leading `@` that is part of the name, so the version separator is the first `@` after the scope: `@scope/server` is unpinned and `@scope/server@1.2.3` is pinned. Tool errors retain stable codes across built-in and MCP execution paths.

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
