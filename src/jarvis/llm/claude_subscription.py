"""Claude Agent SDK implementation of :class:`LLMBackend`, authenticated
through Felix's own Claude Code CLI subscription login rather than a
metered ``ANTHROPIC_API_KEY``.

This backend rides ``claude_agent_sdk.ClaudeSDKClient``, which is the same
mechanism the bundled ``claude`` CLI uses to authenticate: when no API key
is present in the process environment, the SDK spawns the CLI's own
subprocess and inherits whatever session that CLI already has open. There
is no key to store, mask, or rotate for this route.

**Text generation only, never a second acting agent.** Jarvis owns exactly
one tool-calling loop and one security confirmation gate
(``../security/security.spec.md``). ``ClaudeSDKClient`` is otherwise a
fully agentic session with its own tool-calling loop, so every session
opened here is deliberately stripped down to a text generator:

- ``tools=[]`` removes Claude Code's own built-in tools (Read, Bash, Edit,
  and friends) from the session.
- ``setting_sources=[]`` and ``mcp_servers={}`` stop the session from
  loading project or user configuration, including any locally configured
  MCP servers.
- None of the above is sufficient by itself. An authenticated session can
  still see MCP tools that are attached at the Anthropic account level
  (connectors configured in the Claude.ai account the CLI is logged into),
  and those are entirely outside this process's control. The one
  mechanism that reliably stops execution regardless of what the model
  believes it can call is the ``can_use_tool`` callback below, which
  denies every tool-use attempt unconditionally. This was verified
  empirically before this module was written: with an authenticated
  session and no other options set, a "list your tools" prompt returned
  over 250 MCP tools from account-level connectors despite
  ``tools=[]``/``mcp_servers={}``/``setting_sources=[]`` all being set,
  and the model attempted to call one of them on request. Only the
  ``can_use_tool`` deny-all callback actually stopped that attempt before
  it ran.
- ``permission_mode`` is always ``"default"``, never ``"bypassPermissions"``
  or any other mode that auto-approves calls ahead of ``can_use_tool``.
  The SDK's own ``CanUseToolShadowedWarning`` documents that some
  permission modes approve tool calls before the callback ever runs;
  using one of those here would silently defeat the deny-all gate.

Text produced by a session goes back through Jarvis's own agentic loop and
security gate exactly like a local Ollama or Hermes response would: this
backend's job ends at generating text.

**Fresh session per call.** Jarvis's own ``LLMBackend`` contract already
carries the full conversation on every call (``chat()`` receives the whole
``messages`` list; ``direct()`` receives system and user text together).
Resuming or continuing an SDK session across calls would duplicate that
context rather than save anything, so every call here opens a new
``ClaudeSDKClient``, sends one prompt, and disconnects. Nothing about this
backend is stateful between calls.

**Native tool schemas are never supported.** With ``tools=[]`` the model
has nothing it could call, so a caller that supplies ``tools=`` to
:meth:`chat` always gets :class:`ToolsNotSupportedError`, exactly as an
OpenAI-compatible server that rejects a tool schema would. The reply
engine already knows how to fall back to text-based tool calling on this
signal.

**Optional dependency.** ``claude-agent-sdk`` is not listed in
``requirements.txt``: it requires ``mcp>=1.23.0,<3.0.0``, which conflicts
with the ``mcp==1.13.1`` pin the persistent MCP runtime
(``../tools/external/mcp_runtime.spec.md``) depends on. Installing
``claude-agent-sdk`` is an explicit, separate step for anyone who
configures a ``claude_subscription`` route; this module imports it lazily
so the rest of Jarvis works unmodified without it, and a route that tries
to use it without the package installed fails with a typed
:class:`ProviderError` rather than an import crash.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .backend import (
    AuthError,
    LLMBackend,
    ModelUnavailableError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    ToolsNotSupportedError,
)

try:
    import claude_agent_sdk as _sdk
except ImportError:  # optional dependency, see module docstring
    _sdk = None


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
    Code CLI session via ``claude_agent_sdk``. See the module docstring for
    the text-generation-only guarantee and why no API key is used."""

    def _log_denied_tool_use(self, tool_name: str) -> None:
        debug_log(
            "ClaudeSubscriptionBackend: denied a tool-use attempt from the "
            f"SDK side ({tool_name})",
            "llm",
        )

    def _log_selected(self, method: str) -> None:
        debug_log(f"ClaudeSubscriptionBackend: selected for {method}", "llm")

    def _log_session_failure(self, detail: str) -> None:
        debug_log(f"ClaudeSubscriptionBackend: auth/session failure ({detail})", "llm")

    def _build_options(self, model: str, system_prompt: str, on_token):
        async def _deny_all_tool_use(tool_name, tool_input, context):
            self._log_denied_tool_use(tool_name)
            return _sdk.PermissionResultDeny(
                message="text-generation-only backend: tool use is not permitted"
            )

        return _sdk.ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt or None,
            permission_mode="default",
            tools=[],
            can_use_tool=_deny_all_tool_use,
            setting_sources=[],
            mcp_servers={},
            cwd=tempfile.gettempdir(),
            max_turns=1,
            include_partial_messages=on_token is not None,
        )

    async def _converse(
        self,
        model: str,
        system_prompt: str,
        prompt: str,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> str:
        if _sdk is None:
            raise ProviderError("claude-agent-sdk is not installed")

        options = self._build_options(model, system_prompt, on_token)
        client = _sdk.ClaudeSDKClient(options=options)
        text_parts: List[str] = []
        result_message = None
        try:
            await client.connect()
            await client.query(prompt)
            async for message in client.receive_response():
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    for block in getattr(message, "content", []) or []:
                        text = getattr(block, "text", None)
                        if isinstance(text, str) and text:
                            text_parts.append(text)
                elif kind == "StreamEvent" and on_token is not None:
                    event = getattr(message, "event", {}) or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {}) or {}
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                try:
                                    on_token(chunk)
                                except Exception as listener_error:
                                    debug_log(
                                        "ClaudeSubscriptionBackend: token "
                                        f"listener failed ({listener_error})",
                                        "llm",
                                    )
                elif kind == "ResultMessage":
                    result_message = message
                    break
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        if result_message is not None and bool(getattr(result_message, "is_error", False)):
            status = getattr(result_message, "api_error_status", None)
            error = _map_result_error(status if isinstance(status, int) else None)
            if isinstance(error, AuthError):
                self._log_session_failure(f"api_error_status={status}")
            raise error

        return "".join(text_parts)

    def _run(
        self,
        model: str,
        system_prompt: str,
        prompt: str,
        timeout_sec: float,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        try:
            text = asyncio.run(
                asyncio.wait_for(
                    self._converse(model, system_prompt, prompt, on_token),
                    timeout=max(0.1, float(timeout_sec)),
                )
            )
        except asyncio.TimeoutError:
            raise ProviderError("provider request timed out") from None
        except ProviderError:
            raise
        except Exception as error:  # any SDK-raised or transport error
            if _sdk is not None and isinstance(error, _sdk.ClaudeSDKError):
                if isinstance(error, getattr(_sdk, "ResultError", ())):
                    status = getattr(error, "api_error_status", None)
                    mapped = _map_result_error(status)
                    if isinstance(mapped, AuthError):
                        self._log_session_failure(f"api_error_status={status}")
                    raise mapped from None
                if isinstance(error, getattr(_sdk, "CLINotFoundError", ())):
                    self._log_session_failure("claude CLI not found")
                    raise ProviderError("provider is not available") from None
            debug_log(
                f"ClaudeSubscriptionBackend: request failed ({type(error).__name__})",
                "llm",
            )
            raise ProviderError(f"provider request failed ({type(error).__name__})") from None
        return text if text and text.strip() else None

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
