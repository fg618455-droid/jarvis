"""Standalone check: does claude_agent_sdk authenticate through an
already-logged-in Claude Code CLI session, with no ANTHROPIC_API_KEY set,
from a plain non-interactive script?

This is the manual verification behind the `claude_subscription` LLM
route (see src/jarvis/llm/llm.spec.md, "Claude subscription session").
It is not a pytest test: it makes a real network call to Anthropic
through whatever `claude` CLI session is active on this machine, so it
belongs in an interactive terminal, not the automated suite.

Usage (claude-agent-sdk is an optional dependency, install it separately
first — see llm.spec.md for why it is not in requirements.txt):

    pip install claude-agent-sdk
    python scripts/verify_claude_subscription_auth.py

Run this after a `claude` CLI re-login, or after upgrading
claude-agent-sdk, to confirm the subscription-session path still works
before relying on it in Jarvis.
"""
import asyncio
import os
import sys
import time


def main() -> int:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ABORT: ANTHROPIC_API_KEY is set in this process's environment. "
            "Unset it before running this check — the whole point is "
            "proving the subscription session works without one."
        )
        return 1

    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    except ImportError:
        print(
            "claude-agent-sdk is not installed in this environment. "
            "Install it separately (pip install claude-agent-sdk) to run "
            "this check; it is not a Jarvis dependency by default."
        )
        return 1

    async def run() -> None:
        options = ClaudeAgentOptions(
            model="claude-sonnet-4-5-20250929",
            permission_mode="default",
            tools=[],
            setting_sources=[],
            mcp_servers={},
            max_turns=1,
        )
        client = ClaudeSDKClient(options=options)
        t0 = time.monotonic()
        await client.connect()
        try:
            await client.query("Reply with exactly the word: pong")
            text_parts = []
            async for message in client.receive_response():
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
                elif kind == "ResultMessage":
                    elapsed = time.monotonic() - t0
                    is_error = bool(getattr(message, "is_error", False))
                    status = getattr(message, "api_error_status", None)
                    print(f"round_trip_sec={elapsed:.2f}")
                    print(f"response_text={' '.join(text_parts)!r}")
                    print(f"is_error={is_error} api_error_status={status}")
                    if is_error:
                        print(
                            "OUTCOME: session connected, but the API call "
                            "itself failed (see api_error_status above) — "
                            "check the model id and account access."
                        )
                    else:
                        print(
                            "OUTCOME: SUCCESS — subscription-session auth "
                            "works for a non-interactive script with no "
                            "API key set."
                        )
                    break
        finally:
            await client.disconnect()

    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"OUTCOME: FAILURE — {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
