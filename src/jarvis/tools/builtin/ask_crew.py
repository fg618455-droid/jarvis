"""Delegates a task to a specialist agent in the self-hosted Hermes crew.

Jarvis has no direct channel into Hermes and does not run it. The one
channel Hermes already watches and answers through is its own Telegram
group, so this tool posts the task into the target agent's topic there and
returns immediately — it never waits for or reads back Hermes' reply. The
result surfaces to Felix in that Telegram channel or the shared vault, on
Hermes' own time, not inline in this conversation.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...debug import debug_log
from ...telegram.transport import RequestsTelegramTransport
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult


# Fixed Telegram-topic layout of the "Mission Control" group, set up once via
# docs-felix/nas-scripts/topics-erfassen.sh on the Hermes side. Changing the
# group's topics means updating both sides together.
AGENT_THREADS: Dict[str, Optional[int]] = {
    "jarvis": None,  # General topic, no thread id needed
    "dev": 2,
    "research": 5,
    "assistant": 6,
    "schule": 7,
    "scribe": 8,
    "reach": 9,
}


class AskCrewTool(Tool):
    """Delegates a task to one specialist in the Hermes crew, fire-and-forget."""

    @property
    def name(self) -> str:
        return "askCrew"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to a specialist agent running in the self-hosted "
            "Hermes crew (dev, research, assistant, schule, scribe, reach) — "
            "use this for work that needs more time, tool depth, or reasoning "
            "power than a quick local answer, e.g. a multi-step investigation "
            "that would otherwise take too long. The task is posted into that "
            "agent's own channel; the agent works independently and delivers "
            "its result there or in the shared vault, not back into this "
            "conversation."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": sorted(AGENT_THREADS.keys()),
                    "description": "Which crew specialist should take the task.",
                },
                "task": {
                    "type": "string",
                    "description": "The task to delegate, in the user's own words.",
                },
            },
            "required": ["agent", "task"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        agent = str(args.get("agent", "")).strip().lower()
        task = str(args.get("task", "")).strip()

        if agent not in AGENT_THREADS:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_ARGUMENT,
                f"Unknown crew agent '{agent}'. Choose one of: "
                f"{', '.join(sorted(AGENT_THREADS))}.",
            )
        if not task:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_ARGUMENT, "No task text given.",
            )

        cfg = context.cfg
        bot_token = getattr(cfg, "telegram_bot_token", "") or ""
        chat_id = getattr(cfg, "crew_telegram_chat_id", "") or ""
        if not bot_token or not chat_id:
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_CONFIG,
                "The crew channel isn't set up yet — set crew_telegram_chat_id "
                "in Settings under Mission Control.",
            )

        transport = RequestsTelegramTransport(
            bot_token, base_url=getattr(cfg, "telegram_api_base_url", ""),
        )

        payload: Dict[str, Any] = {"chat_id": chat_id, "text": task}
        thread_id = AGENT_THREADS[agent]
        if thread_id is not None:
            payload["message_thread_id"] = thread_id

        try:
            transport.post("sendMessage", payload, timeout=10)
        except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
            debug_log(f"askCrew could not reach the crew channel: {exc}", "tools")
            return ToolExecutionResult.failure(
                ToolErrorCode.UNAVAILABLE,
                "Could not reach the crew channel right now — try again shortly.",
                retryable=True,
            )

        context.user_print(f"📨 Delegated to {agent}: {task[:60]}")
        debug_log(f"askCrew delegated to {agent}", "tools")
        return ToolExecutionResult(
            success=True,
            reply_text=(
                f"Delegated to {agent}. They will post the result in the crew "
                f"channel or the shared vault once done."
            ),
        )
