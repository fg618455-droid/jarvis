"""💬 Tool to open or close a wake-word-free conversation.

Conversation mode has a control path ahead of the reply engine already:
the intent judge decides, in any language, that the user asked to keep
talking. That path is the right one and stays, but it is not always
present. The judge can be unavailable, and text chat and Telegram never
run one at all. A control phrase that gets past it reaches the reply
engine, which can only answer *about* the request, so the user watches
Jarvis discuss a switch instead of flipping it.

This tool is the second way in. The router already understands whatever
language the user speaks, so the mapping from their words to a boolean is
the model's job and no phrase in any language appears in this file.
"""

from typing import Any, Dict, Optional

from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult
from ...debug import debug_log
from ...listening.conversation_mode import set_conversation_mode


class ConversationModeTool(Tool):
    """Turns the wake-word-free listening mode on or off."""

    @property
    def name(self) -> str:
        return "setConversationMode"

    @property
    def description(self) -> str:
        return (
            "Turn conversation mode on or off. In conversation mode the user "
            "can keep talking without saying the wake word before every "
            "sentence. Use this whenever the user asks to switch continuous "
            "or wake-word-free listening on or off, in any language. This is "
            "a control instruction to carry out, not a question to answer: "
            "call the tool rather than describing what the mode does."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": (
                        "True to open a wake-word-free conversation, false "
                        "to close it and require the wake word again."
                    ),
                },
            },
            "required": ["enabled"],
        }

    def run(
        self,
        args: Optional[Dict[str, Any]],
        context: ToolContext,
    ) -> ToolExecutionResult:
        enabled = (args or {}).get("enabled")
        if not isinstance(enabled, bool):
            # Guessing the direction of a switch the user asked about is
            # worse than saying the request was not understood.
            return ToolExecutionResult.failure(
                ToolErrorCode.INVALID_ARGUMENT,
                "Say whether conversation mode should be on or off.",
            )

        if not set_conversation_mode(enabled):
            debug_log(
                "setConversationMode reached no listener", "tools",
            )
            return ToolExecutionResult.failure(
                ToolErrorCode.UNAVAILABLE,
                "Conversation mode needs the voice listener, and nothing is "
                "listening right now.",
            )

        state = "on" if enabled else "off"
        context.user_print(f"💬 Conversation mode {state}")
        debug_log(f"setConversationMode turned conversation mode {state}", "tools")
        return ToolExecutionResult(
            success=True,
            reply_text=f"Conversation mode is now {state}.",
        )
