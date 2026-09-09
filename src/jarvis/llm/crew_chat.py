"""Synchronous :class:`LLMBackend` implementation relaying Tier.CHAT turns
to the Hermes crew's own chat engine, running on Felix's NAS.

This rides the exact wire shape already working in
``jarvis.webui.api.crew``'s ``crew_chat()``: a plain ``POST {base}/chat``
with ``{"agent": ..., "message": ...}`` and an ``X-Crew-Key`` header, relaying
back whatever the NAS-side endpoint proxies from the crew's own chat engine.
That endpoint is Mission Control's own web-UI chat feature and is untouched
by this module; this backend is an independent caller of the same NAS
endpoint family, not a wrapper around the Flask blueprint.

**Text generation only.** Like :class:`~jarvis.llm.claude_subscription.
ClaudeSubscriptionBackend`, this backend has no native tool-calling concept
of its own: the crew's chat engine takes one message and returns one reply,
nothing that resembles the OpenAI tools API. :meth:`chat` raises
:class:`ToolsNotSupportedError` whenever ``tools`` is supplied, before any
request is made, so the reply engine falls back to text-based tool calling
without losing the turn.

**Fail-closed, never a guess.** The route this backend answers for reuses
``cfg.crew_api_url`` / ``cfg.crew_api_key`` (the same fields Mission Control
already reads) plus a new ``cfg.crew_chat_agent`` naming which crew
specialist answers. Neither is duplicated onto the route's own
``base_url`` / ``api_key`` / ``model`` fields, which stay placeholders,
exactly the convention ``claude_subscription`` already uses for its inert
``base_url``. If either the endpoint or the agent is missing, this backend
never falls back to an arbitrary crew agent: every call raises a typed
:class:`ProviderError` instead, which ``RoutedBackend`` treats as an
ordinary route failure and falls through the rest of the chain for.

**Never used for Tier.PRIVATE**, the same invariant every route provider
gets: every configured route entry that resolves to ``Tier.PRIVATE`` is
dropped before a backend is built, for every provider without exception, so
a ``crew_chat`` route can never reach the private lane even if misconfigured
with that tier.

**Typed failures** follow the same table :class:`OpenAICompatibleBackend`
uses (reused directly, not reimplemented): HTTP 401/403 to ``AuthError``,
404 to ``ModelUnavailableError``, 429 to ``RateLimitedError`` or
``QuotaExhaustedError`` depending on the response, anything else to
``ProviderError``. A connection failure or timeout is also a
``ProviderError``. A missing or empty ``reply`` field in an otherwise
successful response is an empty response, not an exception, so
``RoutedBackend`` moves on to the next candidate exactly as it would for any
other backend that produced nothing.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import requests

from ..debug import debug_log
from .backend import (
    LLMBackend,
    ProviderError,
    ToolsNotSupportedError,
)
from .claude_subscription import _flatten_messages
from .openai_compatible import _raise_http_error


def _combine_flattened(system_text: str, prompt: str) -> str:
    """Join the flattened system/user text into the single message string
    the crew chat endpoint expects.

    The endpoint takes one ``message`` field, not a role-tagged history
    (see ``webui/api/crew.py``'s ``crew_chat()``), so the system text (if
    any) and the rendered transcript are joined into one string rather than
    sent separately the way ``ClaudeSDKClient`` wants them.
    """
    parts = [part.strip() for part in (system_text, prompt) if part and part.strip()]
    return "\n\n".join(parts)


class CrewChatBackend(LLMBackend):
    """:class:`LLMBackend` implementation relaying to the NAS-hosted Hermes
    crew's chat engine. See the module docstring for the text-generation-only
    guarantee and the fail-closed behaviour when unconfigured."""

    def __init__(self, base_url: str, api_key: str = "", agent: str = "") -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = api_key or ""
        self._agent = (agent or "").strip().lower()

    def _headers(self) -> Dict[str, str]:
        return {"X-Crew-Key": self._api_key} if self._api_key else {}

    def _log_selected(self, method: str) -> None:
        debug_log(f"CrewChatBackend: selected for {method}", "llm")

    def _request(self, message: str, timeout_sec: float) -> Optional[str]:
        if not self._base_url or not self._agent:
            # Fail closed rather than guess an agent: an unconfigured
            # endpoint or agent is the same "cannot answer" case as any
            # other provider failure, not a silent default pick.
            debug_log(
                "CrewChatBackend: no crew endpoint or agent configured", "llm",
            )
            raise ProviderError("crew chat route is not configured")

        try:
            response = requests.post(
                f"{self._base_url}/chat",
                headers=self._headers(),
                json={"agent": self._agent, "message": message},
                timeout=timeout_sec,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.Timeout:
            debug_log(f"CrewChatBackend: timeout after {timeout_sec}s", "llm")
            raise ProviderError("provider request timed out") from None
        except requests.exceptions.ConnectionError:
            debug_log("CrewChatBackend: connection failed", "llm")
            raise ProviderError("provider request failed (ConnectionError)") from None
        except requests.exceptions.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status in (401, 403):
                debug_log(f"CrewChatBackend: auth failure (status={status})", "llm")
            _raise_http_error(error, tools=False)
        except ProviderError:
            raise
        except Exception as error:
            debug_log(
                f"CrewChatBackend: request failed ({type(error).__name__})", "llm",
            )
            raise ProviderError(
                f"provider request failed ({type(error).__name__})"
            ) from None

        reply = data.get("reply") if isinstance(data, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            return None
        return reply

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
        # No sampling controls exist on the crew's chat endpoint; accepted
        # for signature parity with every other backend and ignored here,
        # the same posture ClaudeSubscriptionBackend takes.
        self._log_selected("direct")
        message = _combine_flattened(system_prompt, user_content)
        return self._request(message, timeout_sec)

    def streaming(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        on_token: Optional[Callable[[str], None]] = None,
        timeout_sec: float = 30.0,
        thinking: bool = False,
    ) -> Optional[str]:
        # The crew endpoint has no incremental streaming shape: the whole
        # reply arrives in one response, so on_token (if given) fires once
        # with the full text rather than per-token.
        self._log_selected("streaming")
        message = _combine_flattened(system_prompt, user_content)
        text = self._request(message, timeout_sec)
        if text and on_token:
            try:
                on_token(text)
            except Exception as listener_error:
                debug_log(
                    f"CrewChatBackend: token listener failed ({listener_error})",
                    "llm",
                )
        return text

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
            debug_log(
                "CrewChatBackend: denied a native tool schema (text-generation-only)",
                "llm",
            )
            raise ToolsNotSupportedError(
                "crew_chat is text-generation only and never accepts native tools"
            )
        self._log_selected("chat")
        system_text, prompt = _flatten_messages(messages)
        message = _combine_flattened(system_text, prompt)
        text = self._request(message, timeout_sec)
        if text is None:
            return None
        if on_token:
            try:
                on_token(text)
            except Exception as listener_error:
                debug_log(
                    f"CrewChatBackend: token listener failed ({listener_error})",
                    "llm",
                )
        assistant_message = {"role": "assistant", "content": text}
        return {"choices": [{"message": assistant_message}], "message": assistant_message}

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
        # No model-listing concept: a crew agent is not a model catalogue.
        return []

    def warm_up(
        self,
        model: str,
        timeout_sec: float = 60.0,
        keep_alive: str = "30m",
    ) -> bool:
        # Nothing to page in over HTTP; a real round trip here would spend
        # a network call at every daemon start for no benefit.
        return True
