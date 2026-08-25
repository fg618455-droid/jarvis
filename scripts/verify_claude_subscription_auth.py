"""Measure one real Claude subscription request through Jarvis's sidecar."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from jarvis.llm.claude_subscription import ClaudeSubscriptionBackend  # noqa: E402
from jarvis.llm.claude_subscription_sidecar_client import (  # noqa: E402
    ClaudeSubscriptionSidecarClient,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Verify subscription authentication through the isolated sidecar."
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        help="Claude model identifier to request",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly the word: pong",
        help="bounded prompt used for the measured generation",
    )
    args = parser.parse_args()

    client = ClaudeSubscriptionSidecarClient()
    backend = ClaudeSubscriptionBackend(client)
    started = time.monotonic()
    try:
        result = backend.direct(
            args.model,
            "Return only the requested answer, without commentary.",
            args.prompt,
            timeout_sec=120.0,
        )
    except Exception as error:
        print(f"❌ Sidecar round trip failed ({type(error).__name__})")
        return 1
    finally:
        client.stop()

    elapsed = time.monotonic() - started
    print(f"⏱️  round_trip_sec={elapsed:.2f}")
    print(f"💬 response_text={result!r}")
    if not result:
        print("❌ The provider returned no text")
        return 1
    print("✅ Subscription authentication works through the isolated sidecar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
