"""Subscription-authenticated Claude text generation behind a sidecar.

The public backend remains an ordinary :class:`LLMBackend`, while the
Claude Agent SDK runs under its own interpreter and dependency set. The
main Jarvis interpreter communicates with it only through newline-delimited
JSON on subprocess pipes, keeping the persistent MCP runtime's package pin
independent from the SDK's incompatible requirement.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .backend import (
    AuthError,
    LLMBackend,
    ModelUnavailableError,
    ProviderError,
    RateLimitedError,
    ToolsNotSupportedError,
)
from .claude_subscription_sidecar_client import (
    ClaudeSidecarError,
    ClaudeSubscriptionSidecarClient,
)


def _map_result_error(api_error_status: Optional[int]) -> ProviderError:
    """Map a failed :class:`ResultMessage`'s ``api_error_status`` to a
    typed failure, mirroring ``openai_compatible._raise_http_error``."""
    if api_error_status in (401, 403):
        return AuthError("provider rejected the active session")
    if api_error_status == 404:
        return ModelUnavailableError("configured model is unavailable")
    if api_error_status == 429:
        return RateLimitedError()
    return ProviderError("provider request failed")


def _flatten_messages(messages: List[Dict[str, Any]]) -> tuple[str, str]:
    """Collapse an OpenAI-shape messages list into ``(system_text, prompt)``.

    ``ClaudeSDKClient.query()`` takes one prompt per call, not a role-tagged
    history, so system-role content becomes the session's system prompt and
    every other turn is rendered as a labelled line in one flattened
    transcript ending on the latest turn. Native ``tool_calls`` never reach
    this helper because :meth:`ClaudeSubscriptionBackend.chat` raises
    :class:`ToolsNotSupportedError` whenever ``tools`` is supplied, so any
    tool-call history already arrives as plain text content from the
    engine's text-based tool-calling fallback.
    """
    system_parts: List[str] = []
    turns: List[str] = []
    role_labels = {"user": "User", "assistant": "Assistant", "tool": "Tool result"}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "") or "")
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        if not text.strip():
            continue
        if role == "system":
            system_parts.append(text)
        else:
            label = role_labels.get(role, role or "Message")
            turns.append(f"{label}: {text}")
    return "\n\n".join(system_parts), "\n\n".join(turns)


class ClaudeSubscriptionBackend(LLMBackend):
    """:class:`LLMBackend` implementation riding an authenticated Claude
    Code CLI session inside an isolated subprocess."""

    def __init__(
        self,
        sidecar_client: Optional[ClaudeSubscriptionSidecarClient] = None,
    ) -> None:
        self._sidecar = sidecar_client or ClaudeSubscriptionSidecarClient()

    def _log_selected(self, method: str) -> None:
        debug_log(f"ClaudeSubscriptionBackend: selected for {method}", "llm")

    def _log_session_failure(self, detail: str) -> None:
        debug_log(f"ClaudeSubscriptionBackend: auth/session failure ({detail})", "llm")

    def _run(
        self,
        model: str,
        system_prompt: str,
        prompt: str,
        timeout_sec: float,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        try:
            return self._sidecar.generate(
                model,
                system_prompt,
                prompt,
                timeout_sec,
                on_token,
            )
        except ClaudeSidecarError as error:
            mapped = _map_result_error(error.status)
            if isinstance(mapped, AuthError):
                self._log_session_failure(f"api_error_status={error.status}")
            else:
                debug_log(
                    "ClaudeSubscriptionBackend: sidecar request failed "
                    f"({type(mapped).__name__})",
                    "llm",
                )
            raise mapped from None
        except Exception as error:
            debug_log(
                "ClaudeSubscriptionBackend: unexpected sidecar client failure "
                f"({type(error).__name__})",
                "llm",
            )
            raise ProviderError("provider request failed") from None

    # -- LLMBackend interface --------------------------------------------

    def direct(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        timeout_sec: float = 10.0,
        thinking: bool = False,
        num_ctx: int = 4096,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        # ``num_ctx``, ``thinking``, ``temperature``, and ``max_tokens`` have
        # no equivalent exposed by ``ClaudeAgentOptions`` for a single
        # generation call; the CLI manages its own sampling and context
        # sizing. Accepted for signature parity with every other backend
        # and silently ignored here, matching how OpenAICompatibleBackend
        # ignores Ollama-only knobs.
        self._log_selected("direct")
        return self._run(chat_model, system_prompt, user_content, timeout_sec)

    def streaming(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        on_token: Optional[Callable[[str], None]] = None,
        timeout_sec: float = 30.0,
        thinking: bool = False,
    ) -> Optional[str]:
        self._log_selected("streaming")
        return self._run(chat_model, system_prompt, user_content, timeout_sec, on_token)

    def chat(
        self,
        chat_model: str,
        messages: List[Dict[str, Any]],
        timeout_sec: float = 30.0,
        extra_options: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        if tools:
            # This backend's session never carries a usable tool (see the
            # module docstring): with tools=[] the model has nothing to
            # call, so a native tool schema can never be satisfied. The
            # reply engine already knows to fall back to text-based tool
            # calling on this signal.
            raise ToolsNotSupportedError(
                "claude_subscription is text-generation only and never accepts native tools"
            )
        self._log_selected("chat")
        system_prompt, prompt = _flatten_messages(messages)
        text = self._run(chat_model, system_prompt, prompt, timeout_sec, on_token)
        if text is None:
            return None
        message = {"role": "assistant", "content": text}
        return {"choices": [{"message": message}], "message": message}

    def embed(
        self,
        text: str,
        model: str,
        timeout_sec: float = 15.0,
    ) -> Optional[List[float]]:
        # No embeddings endpoint; embeddings always stay on loopback Ollama
        # per get_embedding_backend(), so this path is not reachable through
        # normal routing and exists only for interface completeness.
        return None

    def list_models(self, timeout_sec: float = 5.0) -> List[str]:
        # No model-listing endpoint is exposed by the SDK for this session
        # shape; the configured route model is used as-is.
        return []

    def warm_up(
        self,
        model: str,
        timeout_sec: float = 60.0,
        keep_alive: str = "30m",
    ) -> bool:
        # No residency to page in: unlike Ollama, there is nothing to keep
        # warm between calls, and unlike a self-hosted OpenAI-compatible
        # server there is no local reachability worth probing at startup.
        # A real round trip here would spend a network call and add several
        # seconds to every daemon start for no benefit, so this inherits
        # the base class's no-op contract.
        return True
