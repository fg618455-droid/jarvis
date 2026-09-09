# Runtime generations

The process owns one runtime. A turn takes a settings/backend generation and
retains it while subsequent turns can observe a newly published generation.
Settings are deep-copied on installation. Persistence, adapter construction and
publication are serialised; a failed build invokes rollback and retains the
previous generation. Voice, direct text and conversation entry points take the
runtime settings snapshot before executing their reply.

Equal route configurations reuse adapters only when resolved credentials match.
Environment-key rotation changes the salted fingerprint and builds a new adapter;
old turns retain the old credential. Status exposes safe health metadata. Retired
generations remain available until shutdown, when shared adapters close once.

Tests cover rollback, settings isolation, credential rotation, and concurrent
direct/stream/chat calls spanning reconfiguration. These simulated turns do not
substitute for live subscription parser/dispatcher acceptance.
