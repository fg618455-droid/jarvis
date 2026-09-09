# Diagnostics Specification

Diagnostics are local, non-destructive, and dependency-light. They report capability states without opening devices, contacting providers, starting MCP processes, or recording prompts and tool payloads.

Secret-shaped dictionary fields and bearer values are redacted recursively before reports or JSONL events are written. Logging and rotation failures are fail-soft and never affect assistant work. Every report and event carries a correlation ID.

MCP preflight validates transport, platform, command presence, absolute executable paths, and exact npm package versions without launching the configured server.
