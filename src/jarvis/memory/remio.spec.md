# Remio Memory Source Specification

Remio is an optional, local, fail-soft source of attributable note excerpts. The adapter searches notes, reads only the highest-ranked bounded result set, truncates each body, and returns no result when the executable, desktop service, response, or timeout is unavailable.

The adapter does not ask remio to synthesise an answer. The reply engine owns synthesis and receives source-labelled excerpts. Retrieval must never delay the reply beyond its configured timeout or make existing diary and graph memory unavailable.
