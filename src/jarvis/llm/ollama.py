"""Ollama implementation of :class:`LLMBackend`.

This module owns the original behaviour of the previous flat
``src/jarvis/llm.py``: HTTP calls against ``/api/chat``,
``/api/embeddings`` and ``/api/tags``, native tool calling with the
``tools`` parameter (Ollama 0.4+), and the same fail-soft error
handling (return ``None`` on timeouts / connection errors;
:class:`ToolsNotSupportedError` on HTTP 400 with tools).

Nothing about the wire shape, defaults, or response parsing has
changed in this PR — the file is the previous implementation reshaped
into a class so future PRs can drop in OpenAI-compatible and
Anthropic-compatible siblings without touching call sites.
"""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional

import json
import requests

from ..debug import debug_log
from .backend import LLMBackend, ToolsNotSupportedError, strip_nonstandard_message_fields


def check_version(base_url: str, timeout: float = 5.0) -> tuple[bool, str | None]:
    """Probe ``GET /api/version`` and return ``(True, version_str)`` if the
    endpoint responds as an Ollama server, or ``(False, None)`` on failure or
    non-Ollama response."""
    try:
        resp = requests.get(f"{base_url}/api/version", timeout=timeout)
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        if not isinstance(data, dict):
            return False, None
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return False, None
        return True, version
    except Exception:
        return False, None


def extract_text_from_response(data: Dict[str, Any]) -> Optional[str]:
    """Extract text from an LLM chat response across known shapes.

    Handles Ollama's ``message.content`` shape and the OpenAI-style
    ``choices[0].message.content`` / ``choices[0].text`` fallbacks so
    callers do not need to special-case responses that come back from
    OpenAI-compatible runtimes proxied through Ollama.
    """
    if "message" in data and isinstance(data["message"], dict):
        content = data["message"].get("content")
        if isinstance(content, str):
            return content

    if "choices" in data and isinstance(data["choices"], list) and len(data["choices"]) > 0:
        choice = data["choices"][0]
        if isinstance(choice, dict):
            if "message" in choice and isinstance(choice["message"], dict):
                content = choice["message"].get("content")
                if isinstance(content, str):
                    return content
            elif "text" in choice:
                content = choice["text"]
                if isinstance(content, str):
                    return content

    if "content" in data:
        content = data["content"]
        if isinstance(content, str):
            return content

    return None


def _assemble_streamed_chat(
    resp: Any,
    on_token: Callable[[str], None],
) -> Optional[Dict[str, Any]]:
    """Fold an Ollama chat stream back into one response dict.

    Every consumer downstream reads the unstreamed shape, so streaming is an
    arrival schedule rather than a different contract: text is handed to
    ``on_token`` as it lands and the same ``{"message": {...}}`` comes back at
    the end. Reasoning is collected but never reported — it is written for the
    log, not for the speakers.

    ``on_token`` is a side effect on the user's behalf, so a listener that
    raises must not cost them the answer.
    """
    content: List[str] = []
    thinking: List[str] = []
    tool_calls: List[Any] = []
    role = "assistant"

    for line in resp.iter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role") or role
        calls = message.get("tool_calls")
        if calls:
            tool_calls.extend(calls)
        reasoning = message.get("thinking")
        if isinstance(reasoning, str) and reasoning:
            thinking.append(reasoning)
        chunk = message.get("content")
        if not isinstance(chunk, str) or not chunk:
            continue
        content.append(chunk)
        try:
            on_token(chunk)
        except Exception as e:
            debug_log(f"OllamaBackend.chat: token listener failed — {e}", "llm")

    if not content and not tool_calls and not thinking:
        debug_log("OllamaBackend.chat: stream produced nothing", "llm")
        return None

    message: Dict[str, Any] = {"role": role, "content": "".join(content)}
    if thinking:
        message["thinking"] = "".join(thinking)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"message": message, "done": True}


class OllamaBackend(LLMBackend):
    """:class:`LLMBackend` implementation that talks to a local Ollama server."""

    def __init__(self, base_url: str, keep_alive: Optional[str] = None) -> None:
        self._base_url = base_url.rstrip("/")
        # How long Ollama should hold this backend's models in memory after a
        # request. Ollama resets the unload timer from every request's own
        # ``keep_alive`` and applies its short default when the field is
        # absent, so warming a model once is not enough: each call has to
        # renew the residency or an idle stretch costs the next reply a cold
        # page-in. ``None`` leaves the decision to the server.
        self._keep_alive = keep_alive

    @property
    def base_url(self) -> str:
        return self._base_url

    def _with_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Renew model residency unless the caller already asked for a value."""
        if self._keep_alive and "keep_alive" not in payload:
            payload["keep_alive"] = self._keep_alive
        return payload

    # ── chat ───────────────────────────────────────────────────────────

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
        """Direct LLM call without temporal context, location, or other
        ``ask_coach`` features.

        ``num_ctx`` controls Ollama's context window for this call.
        Default 4096 is fine for small classification-shaped passes;
        callers that assemble richer prompts (planner with dialogue +
        memory + tool catalogue) should pass a larger value to avoid
        silent truncation.

        ``temperature`` is forwarded to Ollama when set. Pass ``0.0``
        for classification / extraction calls where determinism beats
        creativity — Ollama defaults to ~0.8 otherwise, which can
        flake small models on rule-following tasks (e.g. the knowledge
        extractor's banned-form list).

        ``max_tokens`` maps to Ollama's ``num_predict``, capping the
        total generated tokens (including reasoning). Essential for
        classification calls where small reasoning models otherwise
        loop endlessly.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        options: Dict[str, Any] = {"num_ctx": num_ctx}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": messages,
            "stream": False,
            "cache_prompt": True,
            "options": options,
            "think": thinking,
        }

        self._with_residency(payload)

        try:
            with requests.post(
                f"{self._base_url}/api/chat", json=payload, timeout=timeout_sec
            ) as resp:
                resp.raise_for_status()
                data = resp.json()

            if isinstance(data, dict):
                content = extract_text_from_response(data)
                if isinstance(content, str) and content.strip():
                    return content
                debug_log(
                    f"OllamaBackend.direct: empty content from response keys={list(data.keys())}",
                    "llm",
                )
        except requests.exceptions.Timeout:
            debug_log(f"OllamaBackend.direct: timeout after {timeout_sec}s", "llm")
            return None
        except Exception as e:
            debug_log(f"OllamaBackend.direct: request failed — {e}", "llm")
            return None

        return None

    def streaming(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        on_token: Optional[Callable[[str], None]] = None,
        timeout_sec: float = 30.0,
        thinking: bool = False,
    ) -> Optional[str]:
        """Streaming LLM call that invokes ``on_token`` for each token
        received. Returns the complete response text, or ``None`` on
        error / empty stream.

        Uses ``with requests.post(...)`` so the streaming response (and
        the underlying TCP connection) is released even if iteration
        exits early via an exception or the caller stops consuming.
        Without this, an aborted stream pinned the connection until GC,
        which could happen many turns later under sustained reply
        load.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": messages,
            "stream": True,
            "cache_prompt": True,
            "options": {"num_ctx": 4096},
            "think": thinking,
        }

        self._with_residency(payload)

        try:
            with requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=timeout_sec,
                stream=True,
            ) as resp:
                resp.raise_for_status()

                full_response: List[str] = []
                for line in resp.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            if "message" in data and isinstance(data["message"], dict):
                                content = data["message"].get("content", "")
                                if content:
                                    full_response.append(content)
                                    if on_token:
                                        on_token(content)
                        except json.JSONDecodeError:
                            continue

                result = "".join(full_response)
                return result if result.strip() else None

        except requests.exceptions.Timeout:
            return None
        except Exception:
            return None

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
        """Send an arbitrary messages array to Ollama and return the
        raw response JSON. Caller is responsible for interpreting
        assistant content (including JSON / tool calls).

        Main agentic chat uses ``num_ctx=8192`` so the system prompt
        (tool list + protocol guidance + memory context) does not
        overflow and force Ollama to truncate the tool schema — small
        models like ``gemma4:e2b`` then fall back to pre-trained
        ``tool_code`` scaffolding instead of producing valid tool calls.

        With ``on_token`` the request is streamed and each piece of
        assistant text is handed over as it arrives, so a caller can start
        speaking the first sentence while the rest is still being written.
        The return value is the same assembled response either way, so
        nothing downstream needs to know which mode ran.
        """
        sanitised = strip_nonstandard_message_fields(messages)
        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": sanitised,
            "stream": on_token is not None,
            "cache_prompt": True,
            "options": {"num_ctx": 8192},
            "think": thinking,
        }
        # ``extra_options`` keys land at the Ollama wire root for known
        # request-level fields (``keep_alive``, ``format``, ``think``); the
        # rest fold into the sampling-options dict. The split lets callers
        # pin per-request keep-alive without learning Ollama's wire shape.
        # ``max_tokens`` is the canonical generation cap across backends —
        # translate it to Ollama's ``num_predict`` so callers don't need to
        # know which knob each server speaks.
        if extra_options and isinstance(extra_options, dict):
            for key, value in extra_options.items():
                if key in {"keep_alive", "format", "think"}:
                    payload[key] = value
                elif key == "max_tokens":
                    payload["options"]["num_predict"] = int(value)
                elif key == "options" and isinstance(value, dict):
                    for inner_key, inner_value in value.items():
                        if inner_key == "max_tokens":
                            payload["options"]["num_predict"] = int(inner_value)
                        else:
                            payload["options"][inner_key] = inner_value
                else:
                    payload["options"][key] = value

        self._with_residency(payload)

        if tools and isinstance(tools, list) and len(tools) > 0:
            payload["tools"] = tools

        try:
            with requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=timeout_sec,
                stream=on_token is not None,
            ) as resp:
                resp.raise_for_status()
                if on_token is not None:
                    return _assemble_streamed_chat(resp, on_token)
                data = resp.json()
            if isinstance(data, dict):
                return data
        except requests.exceptions.Timeout:
            print("  ⏱️ LLM request timed out", flush=True)
            return None
        except requests.exceptions.ConnectionError:
            # Bubble out so callers (e.g. the intent judge) can distinguish
            # "server unreachable" from a transient error and apply their own
            # back-off policy.
            print("  ❌ LLM connection error", flush=True)
            raise
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and tools:
                raise ToolsNotSupportedError(
                    f"Model {chat_model!r} returned HTTP 400 — native tools API not supported"
                )
            status = e.response.status_code if e.response is not None else "?"
            print(f"  ❌ LLM HTTP error (status {status})", flush=True)
            return None
        except Exception as e:
            print(f"  ❌ LLM error ({type(e).__name__})", flush=True)
            return None

        return None

    # ── embeddings & discovery ────────────────────────────────────────

    def embed(
        self,
        text: str,
        model: str,
        timeout_sec: float = 15.0,
    ) -> Optional[List[float]]:
        """Embed ``text`` via Ollama's ``/api/embeddings``."""
        try:
            resp = requests.post(
                f"{self._base_url}/api/embeddings",
                json={"model": model, "prompt": text},
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data.get("embedding")
            if isinstance(vec, list):
                return [float(x) for x in vec]
        except Exception:
            return None
        return None

    def list_models(self, timeout_sec: float = 5.0) -> List[str]:
        """List installed Ollama models via ``GET /api/tags``."""
        try:
            resp = requests.get(f"{self._base_url}/api/tags", timeout=timeout_sec)
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", []) if isinstance(data, dict) else []
            names: List[str] = []
            for m in models:
                if isinstance(m, dict):
                    name = m.get("name")
                    if isinstance(name, str) and name:
                        names.append(name)
            return names
        except Exception:
            return []

    def warm_up(
        self,
        model: str,
        timeout_sec: float = 60.0,
        keep_alive: str = "30m",
    ) -> bool:
        """Probe ``/api/version`` to verify the server is Ollama, then issue a
        minimal ``/api/chat`` request so it loads ``model`` into resident memory
        for the requested ``keep_alive`` duration. The chat-endpoint warmup
        exercises the full inference pipeline (JIT compilation, KV-cache
        allocation) that an empty ``/api/generate`` would not trigger,
        preventing a timeout on the first real intent-judge or reply-engine
        call. ``keep_alive`` is caller-supplied so low power mode can ask for a
        short residency instead of holding the model for half an hour.
        Best-effort: errors are swallowed so callers never crash on warmup
        failure."""
        if not self._base_url or not model:
            return False
        try:
            # Verify the server is actually Ollama before warming up —
            # a non-Ollama HTTP server on the same port could return 200
            # to a chat POST and produce a false positive.
            version_to = min(timeout_sec, 5.0)
            ok, _ = check_version(self._base_url, timeout=version_to)
            if not ok:
                return False

            remaining = max(1.0, timeout_sec - version_to)
            resp = requests.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "ping"},
                    ],
                    "stream": False,
                    "keep_alive": keep_alive,
                    "options": {"num_predict": 1, "temperature": 0.0},
                },
                timeout=remaining,
            )
            return resp.status_code == 200
        except Exception:
            return False
