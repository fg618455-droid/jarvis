"""Reads back what a Hermes crew agent has posted in its own Telegram topic.

The companion to ``askCrew``: that tool posts a task and returns immediately,
this one checks what — if anything — showed up since. The Bot API has no
history endpoint, so this only ever reflects messages the router captured
while it was polling; it cannot retrieve anything from before that.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...telegram.router import get_router
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult
from .ask_crew import AGENT_THREADS


class CheckCrewRepliesTool(Tool):
    """Read-only look at a crew agent's topic, fire-and-forget's counterpart."""

    @property
    def name(self) -> str:
        return "checkCrewReplies"

    @property
    def description(self) -> str:
        return (
            "Check what a specialist agent in the self-hosted Hermes crew "
            "(dev, research, assistant, schule, scribe, reach, jarvis) has "
            "posted in its own channel since Jarvis started watching. Use "
            "this after askCrew to see whether a delegated task has a result "
            "yet — there is no way to see anything that arrived before "
            "listening started."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": sorted(AGENT_THREADS.keys()),
                    "description": "Which crew specialist's channel to check.",
                },
            },
            "required": ["agent"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        agent = str(args.get("agent", "")).strip().lower()

        if agent not in AGENT_THREADS:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_ARGUMENT,
                f"Unknown crew agent '{agent}'. Choose one of: "
                f"{', '.join(sorted(AGENT_THREADS))}.",
            )

        cfg = context.cfg
        chat_id = getattr(cfg, "crew_telegram_chat_id", "") or ""
        if not chat_id:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_CONFIG,
                "The crew channel isn't set up yet — set crew_telegram_chat_id "
                "in Settings under Mission Control.",
            )

        router = get_router(cfg)
        if not router.is_available:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_CONFIG,
                "Telegram isn't configured — the crew channel also needs "
                "telegram_bot_token and telegram_chat_id set.",
            )

        thread_id = AGENT_THREADS[agent]
        router.watch_topic(chat_id, thread_id)
        router.ensure_polling()

        messages = router.get_topic_messages(chat_id, thread_id)
        if not messages:
            return ToolExecutionResult(
                success=True,
                reply_text=f"No replies yet from {agent} in the crew channel.",
            )

        lines = [
            f"- {m.get('from', 'unknown')}: {m.get('text', '')}" for m in messages
        ]
        return ToolExecutionResult(success=True, reply_text="\n".join(lines))
