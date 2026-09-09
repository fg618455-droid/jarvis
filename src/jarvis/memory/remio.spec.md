# Remio Memory Source Specification

Remio is an optional, local, fail-soft source of attributable note excerpts. The adapter searches notes, reads only the highest-ranked bounded result set, truncates each body, and returns no result when the executable, desktop service, response, or timeout is unavailable.

The adapter does not ask remio to synthesise an answer. It returns
`RetrievedSnippet` objects whose `MemoryProvenance` carries the attributable
note title. The reply engine owns synthesis and receives the excerpt text
without placing that title in an ordinary prompt. The title is rendered only
after the user asks for a memory origin and the model invokes
`memoryProvenance`. When `remio_memory_enabled` is true, planner-directed
retrieval starts Remio alongside the diary lookup and waits for it for at most
two seconds within the turn's remaining `RequestDeadline`. Retrieval must
never make existing diary and graph memory unavailable. Missing services,
timeouts, malformed output, and empty results leave the prompt unchanged.
