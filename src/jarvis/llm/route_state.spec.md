# Route health state v2

Route identity is a stable hash of tier, provider, base URL and model, independent
of display name and credentials. State separates attempts, successful visible
answers, provider errors, empty responses, cooldown/deadline skips, stream aborts
and chain exhaustion. Status reads do not increment execution counters.

Configured, next-selectable and last-answering routes are distinct facts. Once a
stream emits visible text, it owns that reply; an abort cannot append another
provider's answer. Reset supports an exact route identifier. Old state and BOM
files are read tolerantly. Persisted counters and error classes contain no keys,
prompts, request bodies or generated text.
