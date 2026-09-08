# Backend privacy lanes

FAST and CHAT admit explicit cloud/subscription routes only. Neither lane creates
an Ollama candidate, local speculative stream, warm-up fallback or legacy endpoint
fallback. Empty credentials are unavailable. PRIVATE accepts loopback Ollama only;
embedding requests also always use loopback Ollama. Unknown providers fail closed.

Config v7 removes local FAST/CHAT entries and legacy local model/fallback fields,
normalises the obsolete Ollama chat override to auto, and preserves explicit
credentialed public HTTPS endpoints without probing them during migration.
Legacy LOCAL routing output maps to DEFAULT. Cloud exhaustion produces the fixed
honest unavailable response; background contexts retain fail-soft behaviour.

Memory creation, graph category selection, school-note import, diary, memory,
tool-result and loop summaries resolve PRIVATE, including callers supplying an
obsolete cloud model. Retrieval snippets may still enter the explicit CHAT reply.
The setup wizard persists only the PRIVATE Ollama model and requires the PRIVATE
and embedding models, independently of the legacy chat provider.

Config persistence uses atomic replacement. Windows sharing/lock violations are
retried with a bounded delay; other errors fail without replacing the old file.
Diagnostics contain the exception class and Windows code, never config values.
