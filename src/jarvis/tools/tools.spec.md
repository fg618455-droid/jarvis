# Tool Runtime Specification

Every tool returns `ToolExecutionResult`. Failures carry a stable `ToolErrorCode`, a user-safe message, an execution phase, retryability, optional technical metadata, and a correlation ID. Raw exception text and credentials are not placed in the user-facing message.

`fetchWebPage` accepts HTTP and HTTPS only. It resolves every hostname before connecting and rejects loopback, private, link-local, multicast, reserved, unspecified, and metadata-service addresses. Redirects are followed manually and every destination is validated again. Responses are streamed with byte and text limits.

`screenshot` selects a platform capture adapter, stores the image in a temporary directory, and returns OCR text only. Missing capture or OCR dependencies, permission denial, timeout, capture failure, and empty OCR results are explicit failures rather than successful empty output.

MCP npm catalogue entries use exact versions. Tool errors retain stable codes across built-in and MCP execution paths.

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
