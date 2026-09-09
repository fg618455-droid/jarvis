# Provider acceptance

Probes report credentials, exact model availability, visible chat, streaming and
time to first text, plus native-tool or text-tool support as separate facts.
Only model identifiers, booleans, timings and stable error classes are returned;
credentials, prompts and response bodies are omitted. Billing failures (including
HTTP 402), quota exhaustion, missing models and empty responses stay distinct.

The FCC importer writes only candidates that passed the required probes, retaining
environment-variable names rather than resolved credentials. Unavailable catalogue
entries are not active routes. Existing free-tier/subscription boundaries apply.

Groq GPT-OSS requests use max_completion_tokens with at least 1024 tokens and low
reasoning effort by default, allowing a visible answer after reasoning. An
explicit reasoning setting is retained; other providers keep their request shape.
See the [Groq API reference](https://console.groq.com/docs/api-reference).

`scripts/live_provider_matrix.py` requires `JARVIS_LIVE_ACCEPTANCE=1` and can send
the registered tool schemas together with a harmless named probe tool. A text-tool
format probe does not substitute for parser-and-dispatcher end-to-end acceptance.
