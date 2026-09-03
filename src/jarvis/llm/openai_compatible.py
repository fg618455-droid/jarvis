"""OpenAI-compatible implementation of :class:`LLMBackend`.

Targets any local server that exposes the OpenAI Chat Completions
shape: LM Studio, oMLX, llama.cpp's ``llama-server``, vLLM, LocalAI,
and similar. The wire shape differs from Ollama in three important
ways, all hidden inside this module so callers see one response
shape:

1. **Endpoints** are ``/chat/completions`` and ``/embeddings`` rather
   than ``/api/chat`` and ``/api/embeddings``. Model listing is at
   ``/models`` rather than ``/api/tags``.
2. **Streaming uses Server-Sent Events** (``data: {...}\\n\\n`` with a
   ``data: [DONE]`` terminator) instead of Ollama's JSON-lines.
3. **Tool-call arguments arrive as a JSON-encoded string**
   (``"{\\"x\\": 1}"``) rather than a dict; the reply engine expects
   a dict, so :meth:`chat` decodes them. The same method also lifts
   ``choices[0].message`` to top-level ``message`` so the engine's
   existing parsing path works without branching on provider.

The error handling and ``ToolsNotSupportedError`` semantics mirror
:class:`OllamaBackend` so callers get a single contract regardless of
which backend is active.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional

import json
import requests
import re
import time

from ..debug import debug_log
from .backend import (
    AuthError,
    LLMBackend,
    ModelUnavailableError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    ToolsNotSupportedError,
    strip_nonstandard_message_fields,
)


def _header(headers: Any, name: str) -> Optional[str]:
    if not headers:
        return None
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower() and value is not None:
            return str(value).strip()
    return None


def _duration_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    text = value.strip().lower()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", text)
    if match:
        amount = float(match.group(1))
        return amount * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _reset_timestamp(headers: Any) -> Optional[float]:
    for name in (
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "x-ratelimit-reset",
    ):
        value = _header(headers, name)
        if not value:
            continue
        try:
            numeric = float(value)
            if numeric > 1_000_000_000:
                return numeric
        except ValueError:
            pass
        delay = _duration_seconds(value)
        if delay is not None:
            return time.time() + delay
    return None


def _safe_error_payload(response: Any) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error", data)
    if not isinstance(error, dict):
        return ""
    return " ".join(
        str(error.get(key, "") or "") for key in ("type", "code", "message")
    ).lower()


def _raise_http_error(error: requests.exceptions.HTTPError, *, tools: bool) -> None:
    response = error.response
    status = response.status_code if response is not None else None
    if status == 400 and tools:
        raise ToolsNotSupportedError("native tools API is not supported") from None
    if status in (401, 403):
        raise AuthError("provider rejected the configured credential") from None
    if status == 404:
        raise ModelUnavailableError("configured model is unavailable") from None
    if status == 429:
        payload = _safe_error_payload(response)
        quota = any(marker in payload for marker in (
            "quota_exceeded", "quota exceeded", "quota exhausted", "insufficient_quota"
        ))
        if quota:
            raise QuotaExhaustedError(_reset_timestamp(response.headers)) from None
        retry_after = _duration_seconds(_header(response.headers, "Retry-After"))
        if retry_after is None:
            reset = _reset_timestamp(response.headers)
            retry_after = max(0.0, reset - time.time()) if reset is not None else None
        raise RateLimitedError(retry_after) from None
    raise ProviderError(f"provider HTTP error ({status if status is not None else 'unknown'})") from None


@dataclass
class ServerCapabilities:
    """What an OpenAI-compatible server can actually do, probed with real
    requests. ``reachable`` is False when the server did not respond at all
    (wrong URL, server down); the per-feature flags are only meaningful when
    ``reachable`` is True. ``models`` is the advertised model list."""

    reachable: bool = False
    chat: bool = False
    tools: bool = False
    embeddings: bool = False
    models: List[str] = field(default_factory=list)


def _assemble_streamed_chat(
    resp: Any,
    on_token: Callable[[str], None],
    *,
    capped_by_caller: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fold an OpenAI-shape chat stream back into one response dict.

    Deltas carry fragments rather than whole messages, and a tool call is
    split across chunks that identify themselves by ``index``, so the call is
    rebuilt slot by slot and its argument string decoded once at the end —
    the same shape ``_normalise_response`` produces for an unstreamed reply.

    ``on_token`` is a side effect on the user's behalf, so a listener that
    raises must not cost them the answer.

    A fold that only concatenates deltas cannot tell a finished reply from
    one whose connection dropped halfway, so it also watches for the
    stream's terminal marker: either a ``finish_reason`` or the ``[DONE]``
    sentinel. Text that arrived without one is a severed stream, and
    :func:`_raise_if_incomplete` reports it as a route failure rather than
    handing a fragment back as an answer.
    """
    content: List[str] = []
    role = "assistant"
    calls: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    terminated = False

    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            terminated = True
            continue
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str) and reason:
            finish_reason = reason
            terminated = True
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        role = delta.get("role") or role
        for fragment in delta.get("tool_calls") or []:
            if not isinstance(fragment, dict):
                continue
            slot = calls.setdefault(
                int(fragment.get("index", len(calls))),
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if fragment.get("id"):
                slot["id"] = fragment["id"]
            if fragment.get("type"):
                slot["type"] = fragment["type"]
            func = fragment.get("function")
            if isinstance(func, dict):
                if func.get("name"):
                    slot["function"]["name"] = func["name"]
                if isinstance(func.get("arguments"), str):
                    slot["function"]["arguments"] += func["arguments"]
        chunk = delta.get("content")
        if not isinstance(chunk, str) or not chunk:
            continue
        content.append(chunk)
        try:
            on_token(chunk)
        except Exception as e:
            debug_log(f"OpenAICompatibleBackend.chat: token listener failed — {e}", "llm")

    if not content and not calls:
        debug_log("OpenAICompatibleBackend.chat: stream produced nothing", "llm")
        return None

    if not terminated:
        debug_log(
            "OpenAICompatibleBackend.chat: stream ended without a terminal "
            "marker, treating the partial reply as a route failure",
            "llm",
        )
        raise ProviderError("provider stream ended before the reply did")
    _raise_if_incomplete(finish_reason, capped_by_caller=capped_by_caller)

    message: Dict[str, Any] = {"role": role, "content": "".join(content)}
    if calls:
        message["tool_calls"] = [
            {**call, "function": {**call["function"],
                                  "arguments": _decode_tool_arguments(call["function"]["arguments"])}}
            for _, call in sorted(calls.items())
        ]
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "message": message,
    }


def _raise_if_incomplete(
    finish_reason: Optional[str],
    *,
    capped_by_caller: bool,
) -> None:
    """Refuse a reply the server stopped mid-sentence.

    ``length`` means the model ran out of room, not that it finished. The
    text is a fragment, and a fragment presented as an answer is worse than
    no answer: the user has no way to see that the rest was cut off. A cap
    the caller asked for is the caller's own decision and stands.
    """
    if capped_by_caller or finish_reason != "length":
        return
    debug_log(
        "OpenAICompatibleBackend.chat: reply truncated at the token cap, "
        "treating it as a route failure rather than an answer",
        "llm",
    )
    raise ProviderError("provider truncated the reply")


def _decode_tool_arguments(arguments: str) -> Any:
    """Decode a tool call's accumulated argument string, or keep it as text."""
    try:
        return json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments


def _normalise_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Lift OpenAI's ``choices[0].message`` to top-level ``message``
    (matching Ollama's shape) and JSON-decode any tool-call arguments.

    If the server already returns Ollama's shape (some hybrid servers
    expose both endpoints), the response is passed through unchanged.

    Scope: this helper is OpenAI-shape-specific. Other providers
    (Anthropic, etc.) need their own normaliser inside their own
    backend module — Anthropic's content-block + ``tool_use`` shape
    diverges enough that sharing one normaliser would be more
    confusing than useful. Keep one normaliser per backend.
    """
    if "message" in data and isinstance(data["message"], dict):
        return data

    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict):
            normalised = dict(data)
            decoded_msg = dict(msg)
            tool_calls = decoded_msg.get("tool_calls")
            if isinstance(tool_calls, list):
                decoded_calls: List[Dict[str, Any]] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        decoded_calls.append(tc)
                        continue
                    decoded_tc = dict(tc)
                    func = decoded_tc.get("function")
                    if isinstance(func, dict):
                        decoded_func = dict(func)
                        args = decoded_func.get("arguments")
                        if isinstance(args, str):
                            try:
                                decoded_func["arguments"] = json.loads(args)
                            except (json.JSONDecodeError, ValueError):
                                # Leave as-is; the engine's content-mode
                                # parser may still recover something.
                                pass
                        decoded_tc["function"] = decoded_func
                    decoded_calls.append(decoded_tc)
                decoded_msg["tool_calls"] = decoded_calls
            normalised["message"] = decoded_msg
            return normalised

    return data


class OpenAICompatibleBackend(LLMBackend):
    """:class:`LLMBackend` implementation for OpenAI-compatible servers."""

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

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
        # ``num_ctx`` and ``thinking`` have no equivalent in the OpenAI
        # shape; servers that need a fixed context window configure it
        # at load time, and reasoning is a model attribute rather than
        # a request flag. Both are accepted for signature parity with
        # OllamaBackend and silently ignored here.
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        try:
            with requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=timeout_sec,
            ) as resp:
                resp.raise_for_status()
                data = resp.json()

            normalised = _normalise_response(data) if isinstance(data, dict) else None
            if normalised:
                msg = normalised.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        return content
                debug_log(
                    "OpenAICompatibleBackend.direct: empty response content",
                    "llm",
                )
        except requests.exceptions.Timeout:
            debug_log(f"OpenAICompatibleBackend.direct: timeout after {timeout_sec}s", "llm")
            raise ProviderError("provider request timed out") from None
        except requests.exceptions.ConnectionError:
            raise
        except requests.exceptions.HTTPError as error:
            _raise_http_error(error, tools=False)
        except ProviderError:
            raise
        except Exception as e:
            # The exception string can embed the full URL (and any query-string
            # credentials); log only the class so nothing sensitive leaks.
            debug_log(f"OpenAICompatibleBackend.direct: request failed ({type(e).__name__})", "llm")
            raise ProviderError(f"provider request failed ({type(e).__name__})") from None

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
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": messages,
            "stream": True,
        }

        try:
            with requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=timeout_sec,
                stream=True,
            ) as resp:
                resp.raise_for_status()

                full_response: List[str] = []
                finish_reason: Optional[str] = None
                terminated = False
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
                    if not line.startswith("data:"):
                        # SSE comments (``: ping``) and unrelated lines.
                        continue
                    payload_str = line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        terminated = True
                        break
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices:
                        continue
                    reason = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
                    if isinstance(reason, str) and reason:
                        finish_reason = reason
                        terminated = True
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        full_response.append(content)
                        if on_token:
                            on_token(content)

                result = "".join(full_response)
                if not result.strip():
                    return None
                if not terminated:
                    debug_log(
                        "OpenAICompatibleBackend.streaming: stream ended "
                        "without a terminal marker",
                        "llm",
                    )
                    raise ProviderError("provider stream ended before the reply did")
                _raise_if_incomplete(finish_reason, capped_by_caller=False)
                return result
        except requests.exceptions.Timeout:
            raise ProviderError("provider request timed out") from None
        except requests.exceptions.ConnectionError:
            raise
        except requests.exceptions.HTTPError as error:
            _raise_http_error(error, tools=False)
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(f"provider request failed ({type(error).__name__})") from None

    @staticmethod
    def _encode_tool_call_arguments(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """JSON-encode ``tool_calls[*].function.arguments`` in assistant messages.

        The OpenAI API spec requires ``arguments`` to be a JSON string, but
        ``normalise_openai_response`` decodes it to a dict for internal use.
        When that assistant message is sent back to the server on the next
        turn, we must re-encode it.
        """
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            tc_list = msg.get("tool_calls")
            if not isinstance(tc_list, list):
                continue
            for tc in tc_list:
                func = tc.get("function")
                if not isinstance(func, dict):
                    continue
                args = func.get("arguments")
                if isinstance(args, dict):
                    func["arguments"] = json.dumps(args)
        return messages

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
        """With ``on_token`` the request is streamed and each piece of
        assistant text is handed over as it arrives. The return value is the
        same assembled response either way. See :meth:`LLMBackend.chat`."""
        sanitised = strip_nonstandard_message_fields(messages)
        sanitised = self._encode_tool_call_arguments(sanitised)
        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": sanitised,
            "stream": on_token is not None,
        }
        if extra_options and isinstance(extra_options, dict):
            # ``temperature``, ``max_tokens``, ``top_p`` etc. live at the
            # payload root in the OpenAI shape, not under an ``options``
            # nest. Ollama-only knobs (``keep_alive``, ``num_ctx``,
            # ``num_predict``, ``think``) are silently dropped — they have
            # no equivalent in the OpenAI shape and would 400 against most
            # servers. Sampling fields nested under ``options`` are lifted
            # to the payload root.
            for key, value in extra_options.items():
                if key in {"keep_alive", "num_ctx", "num_predict", "think"}:
                    continue
                if key == "options" and isinstance(value, dict):
                    for inner_key, inner_value in value.items():
                        if inner_key in {"num_ctx", "num_predict"}:
                            continue
                        payload[inner_key] = inner_value
                else:
                    payload[key] = value
        if tools and isinstance(tools, list) and len(tools) > 0:
            payload["tools"] = tools

        try:
            with requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=timeout_sec,
                stream=on_token is not None,
            ) as resp:
                resp.raise_for_status()
                if on_token is not None:
                    return _assemble_streamed_chat(
                        resp, on_token,
                        capped_by_caller="max_tokens" in payload,
                    )
                data = resp.json()
            if isinstance(data, dict):
                choices = data.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    _raise_if_incomplete(
                        choices[0].get("finish_reason"),
                        capped_by_caller="max_tokens" in payload,
                    )
                return _normalise_response(data)
        except requests.exceptions.Timeout:
            debug_log(f"OpenAICompatibleBackend.chat: timeout after {timeout_sec}s", "llm")
            raise ProviderError("provider request timed out") from None
        except requests.exceptions.ConnectionError:
            # ConnectionError messages embed the configured URL via the
            # underlying urllib3 exception, which can leak account-bearing
            # query strings to stdout. Print only the failure mode and
            # bubble the exception so callers (e.g. the intent judge) can
            # distinguish "server unreachable" from a transient HTTP error.
            print("  ❌ LLM connection error", flush=True)
            raise
        except requests.exceptions.HTTPError as error:
            _raise_http_error(error, tools=bool(tools))
        except ProviderError:
            raise
        except Exception as e:
            # Generic exception messages can carry whatever the caller embedded
            # (URLs, tokens). Print only the exception class so the user knows
            # *something* failed without leaking what.
            debug_log(f"OpenAICompatibleBackend.chat: request failed ({type(e).__name__})", "llm")
            raise ProviderError(f"provider request failed ({type(e).__name__})") from None

        return None

    # ── embeddings & discovery ────────────────────────────────────────

    def embed(
        self,
        text: str,
        model: str,
        timeout_sec: float = 15.0,
    ) -> Optional[List[float]]:
        try:
            resp = requests.post(
                f"{self._base_url}/embeddings",
                json={"model": model, "input": text},
                headers=self._headers(),
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            arr = data.get("data") if isinstance(data, dict) else None
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                vec = arr[0].get("embedding")
                if isinstance(vec, list):
                    return [float(x) for x in vec]
        except Exception:
            return None
        return None

    def list_models(self, timeout_sec: float = 5.0) -> List[str]:
        try:
            resp = requests.get(
                f"{self._base_url}/models",
                headers=self._headers(),
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            arr = data.get("data", []) if isinstance(data, dict) else []
            names: List[str] = []
            for m in arr:
                if isinstance(m, dict):
                    name = m.get("id")
                    if isinstance(name, str) and name:
                        names.append(name)
            return names
        except requests.exceptions.Timeout:
            raise ProviderError("provider request timed out") from None
        except requests.exceptions.ConnectionError:
            raise
        except requests.exceptions.HTTPError as error:
            _raise_http_error(error, tools=False)
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(f"provider request failed ({type(error).__name__})") from None
        return []

    def warm_up(
        self,
        model: str,
        timeout_sec: float = 60.0,
        keep_alive: str = "30m",
    ) -> bool:
        """Warm up the model by sending a minimal inference request.

        Phase 1 (reachability check): calls ``GET /models`` to confirm
        the server is up and has models loaded. Fast (capped at 25 % of
        the budget, max 5 s).

        Phase 2 (model loading): sends a single-token chat completion
        (``max_tokens=1``) so the runtime actually loads the model into
        memory. Without this, an OpenAI-compatible server may leave the
        model cold until the first real request, incurring latency on the
        user's first query. This mirrors what ``OllamaBackend.warm_up()``
        does.

        ``keep_alive`` is accepted for signature parity with
        ``OllamaBackend.warm_up`` but ignored: OpenAI-compatible servers
        manage model residency at server load time and have no per-call
        keep-alive knob.

        Best-effort: errors are swallowed; ``False`` is returned when the
        server is unreachable, the model name is missing, or the inference
        request fails, so the listener can warn the user early."""
        if not self._base_url or not model:
            return False

        # Phase 1: reachability probe (fast).
        list_to = min(max(timeout_sec * 0.25, 1.0), 5.0)
        try:
            models = self.list_models(timeout_sec=list_to)
        except Exception:
            return False
        if not models:
            return False

        # Phase 2: minimal inference to force model loading.
        remaining = max(0.1, timeout_sec - list_to)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        try:
            with requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=remaining,
            ) as resp:
                return resp.ok
        except Exception:
            return False

    def check_capabilities(
        self,
        chat_model: str,
        embed_model: Optional[str] = None,
        timeout_sec: float = 8.0,
    ) -> ServerCapabilities:
        """Probe what the server can actually do with real requests: list its
        models, send a tiny chat completion, try a trivial tool call, and ask
        for an embedding. Returns raw booleans (formatting is the caller's
        job). Never raises — every failure mode collapses to a False flag so
        the setup wizard and startup check can report honestly.

        ``chat`` covers both a plain reply and a tool-call-only reply (an empty
        ``content`` with ``tool_calls`` still proves the chat endpoint works)."""
        try:
            models = self.list_models(timeout_sec=timeout_sec)
        except Exception:
            models = []
        caps = ServerCapabilities(models=models)
        if caps.models:
            caps.reachable = True

        # Cap generation: we only need to know the endpoint answers, so a short
        # reply keeps the probe fast on large models and avoids a long
        # generation tripping the timeout and reporting a false "chat broken".
        probe = [{"role": "user", "content": "ping"}]
        probe_opts = {"max_tokens": 16}
        try:
            resp = self.chat(chat_model, probe, timeout_sec=timeout_sec, extra_options=probe_opts)
            if isinstance(resp, dict):
                caps.reachable = True
                msg = resp.get("message")
                msg = msg if isinstance(msg, dict) else {}
                caps.chat = bool((msg.get("content") or "").strip()) or bool(msg.get("tool_calls"))
        except requests.exceptions.ConnectionError:
            # Server unreachable — nothing else can succeed either.
            caps.reachable = False
            return caps
        except Exception:
            pass

        if caps.chat:
            trivial_tool = [{
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "A no-op used to probe tool support.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
            try:
                tool_resp = self.chat(chat_model, probe, tools=trivial_tool,
                                       timeout_sec=timeout_sec, extra_options=probe_opts)
                caps.tools = isinstance(tool_resp, dict)
            except ToolsNotSupportedError:
                caps.tools = False
            except Exception:
                caps.tools = False

        em = (embed_model or "").strip() or chat_model
        if self.embed("ping", em, timeout_sec=timeout_sec):
            caps.embeddings = True
            caps.reachable = True

        return caps
