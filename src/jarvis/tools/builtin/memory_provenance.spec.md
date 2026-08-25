# Memory Provenance Tool Specification

`memoryProvenance` is a read-only built-in tool for questions about the origin
of a remembered fact. Its description declares the semantic intent in
language-independent terms, so the ordinary tool router decides when it is
relevant without matching user phrasing in Python.

The tool receives the retrieved snippets retained for the memory-backed reply,
plus an unrecorded-context marker for the warm profile and hot conversation.
It returns raw JSON with `status`, `records`, and `unrecorded_snippet_count`.
Each record contains the retrieved snippet and one source shape: diary date,
graph node id plus fixed branch, vault-relative path, or Remio note title. It
does not compose a sentence for the user. The raw policy fields are
`source_claim_policy: cite_matching_records_only` and
`missing_origin_policy: report_not_recorded_without_inference`.

When no carried source exists, `status` is `not_recorded` and `records` is
empty. A mix of sourced and unrecorded context returns `partial`. The reply
model cites a record only when its snippet supports the questioned fact;
otherwise it reports that the origin is not recorded. It must not infer a
source from the fact text, warm profile, hot conversation window, or plausible
local filenames and dates.

Vault identifiers are rendered only when they are relative, contain no parent
traversal, have no drive or absolute root, and contain no control characters.
Invalid identifiers produce `{"kind": "vault", "path_status": "invalid"}`;
the hostile value is not echoed.

Provenance remains in Python objects during ordinary replies. Graph node ids,
vault paths, and Remio titles enter an LLM prompt only in this tool's result,
after the user has asked for the origin and the model has invoked the tool.
Diary text retains its existing date prefix for recency handling; attaching
the same date as typed provenance adds no prompt text.

Using the repository's four-characters-per-token estimator, four representative
source fields occupy 192 characters, about 48 tokens, if rendered inline on
every enriched turn. Keeping those fields on Python objects makes their
ordinary-turn prompt cost zero. A representative four-source tool payload is
573 characters, about 144 tokens, and is paid only for an origin request. The
always-on refusal rule is 602 characters, about 151 tokens.

The provenance tool call and result are excluded from `DialogueMemory` tool
carryover. A vault path stated in the visible answer is replaced with a local
placeholder before that assistant reply enters the hot conversation window.
The user receives the requested path on that turn, while a later turn does not
send it to a configured CHAT route without a fresh provenance request.
