"""Secret-free capability probes for configured LLM routes.

The probe records only booleans, model identifiers, timings and stable error
classes. Prompts, generated text, response bodies and credentials never enter
its result or logs.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values

from .backend import (
    AuthError,
    BillingError,
    ModelUnavailableError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    ToolsNotSupportedError,
)
from .openai_compatible import OpenAICompatibleBackend
from .route import Route, _build_backend
from .route_catalogue import ENDPOINTS, EndpointTemplate
from .tiers import Tier

SAFE_ERROR_CLASSES = frozenset({
    "auth", "billing", "quota", "model_missing", "timeout", "transport",
    "empty_response",
})

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "probe_echo",
        "description": "Return the supplied harmless probe value.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    },
}


def configure_cli_output() -> None:
    """Make CLI output reliable on legacy Windows code pages."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def fcc_env_path() -> Path:
    return Path.home() / ".fcc" / ".env"


def load_fcc_values(path: Path | None = None) -> dict[str, str]:
    source = path or fcc_env_path()
    if not source.exists():
        return {}
    return {
        str(key): str(value).strip()
        for key, value in dotenv_values(source).items()
        if key and value is not None and str(value).strip()
    }


def classify_probe_error(error: BaseException | str | None) -> str | None:
    """Reduce an exception to a stable, non-sensitive public category."""
    if error is None:
        return None
    if isinstance(error, str):
        return "empty_response" if "empty" in error.lower() else "transport"
    if isinstance(error, AuthError):
        return "auth"
    if isinstance(error, BillingError):
        return "billing"
    if isinstance(error, (QuotaExhaustedError, RateLimitedError)):
        return "quota"
    if isinstance(error, ModelUnavailableError):
        return "model_missing"
    if isinstance(error, (TimeoutError, requests.exceptions.Timeout)):
        return "timeout"
    if isinstance(error, requests.exceptions.RequestException):
        return "transport"
    if isinstance(error, ProviderError):
        lowered = str(error).lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout"
        if "empty" in lowered:
            return "empty_response"
        return "transport"
    return "transport"


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 2)


def _visible_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_tool_call(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    message = response.get("message")
    if not isinstance(message, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
    if not isinstance(message, dict):
        return False
    calls = message.get("tool_calls")
    return bool(isinstance(calls, list) and any(
        isinstance(call, dict)
        and isinstance(call.get("function"), dict)
        and call["function"].get("name") == "probe_echo"
        for call in calls
    ))


def _looks_like_text_tool_call(value: Any) -> bool:
    if not _visible_text(value):
        return False
    text = str(value).strip().replace("```json", "").replace("```", "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        parsed = json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return False
    return (
        isinstance(parsed, dict)
        and parsed.get("tool") == "probe_echo"
        and isinstance(parsed.get("arguments"), dict)
    )


def _credential_status(route: Route, environ: Mapping[str, str]) -> dict[str, Any]:
    required = route.provider == "openai_compatible"
    source = "none"
    present = not required
    if route.api_key_env:
        source = "environment"
        present = bool(str(environ.get(route.api_key_env, "") or "").strip())
    elif route.api_key:
        source = "inline"
        present = bool(route.api_key.strip())
    return {"required": required, "present": present, "source": source}


def probe_route(
    route: Route,
    *,
    backend=None,
    environ: Mapping[str, str] | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Probe one exact route without retaining any generated content."""
    env = os.environ if environ is None else environ
    credential = _credential_status(route, env)
    timeout = max(0.1, min(float(timeout_sec or route.timeout_sec), 45.0))
    result: dict[str, Any] = {
        "name": route.name,
        "provider": route.provider,
        "tier": route.tier.value,
        "model": route.model,
        "configured": bool(route.enabled and credential["present"]),
        "credential": credential,
        "model_listing": {
            "supported": route.provider == "openai_compatible",
            "ok": False,
            "count": 0,
        },
        "model_exact": False,
        "models": [],
        "chat": {"ok": False, "latency_ms": None, "error": None},
        "stream": {"ok": False, "latency_ms": None, "ttft_ms": None, "error": None},
        "tools": {
            "native": False,
            "text_fallback": False,
            "latency_ms": None,
            "error": None,
        },
        "ok": False,
        "error_class": None,
        "import_candidate": False,
    }
    if not result["configured"]:
        return result

    concrete = backend or _build_backend(route)
    errors: list[str] = []

    if result["model_listing"]["supported"]:
        started = time.monotonic()
        try:
            models = concrete.list_models(timeout_sec=timeout)
            result["models"] = list(models)
            result["model_listing"].update({
                "ok": bool(models),
                "count": len(models),
                "latency_ms": _elapsed_ms(started),
            })
            result["model_exact"] = route.model in models
        except Exception as error:  # immediately reduced; never serialized
            category = classify_probe_error(error)
            result["model_listing"]["error"] = category
            errors.append(category or "transport")
    else:
        result["model_exact"] = True

    started = time.monotonic()
    try:
        chat = concrete.direct(
            route.model,
            "Return one visible word.",
            "Capability probe.",
            timeout_sec=timeout,
            temperature=0.0,
            max_tokens=64,
        )
        visible = _visible_text(chat)
        result["chat"].update({
            "ok": visible,
            "latency_ms": _elapsed_ms(started),
            "error": None if visible else "empty_response",
        })
        if not visible:
            errors.append("empty_response")
    except Exception as error:
        category = classify_probe_error(error)
        result["chat"].update({"latency_ms": _elapsed_ms(started), "error": category})
        errors.append(category or "transport")

    if "stream" in route.capabilities:
        started = time.monotonic()
        first_token_at: list[float] = []

        def on_token(token: str) -> None:
            if token and token.strip() and not first_token_at:
                first_token_at.append(time.monotonic())

        try:
            streamed = concrete.streaming(
                route.model,
                "Return one visible word.",
                "Streaming capability probe.",
                on_token=on_token,
                timeout_sec=timeout,
            )
            visible = _visible_text(streamed)
            result["stream"].update({
                "ok": visible,
                "latency_ms": _elapsed_ms(started),
                "ttft_ms": (
                    round((first_token_at[0] - started) * 1000.0, 2)
                    if first_token_at else None
                ),
                "error": None if visible else "empty_response",
            })
            if not visible:
                errors.append("empty_response")
        except Exception as error:
            category = classify_probe_error(error)
            result["stream"].update({"latency_ms": _elapsed_ms(started), "error": category})
            errors.append(category or "transport")

    if "tools" in route.capabilities:
        started = time.monotonic()
        try:
            response = concrete.chat(
                route.model,
                [{"role": "user", "content": "Call probe_echo with value ok."}],
                timeout_sec=timeout,
                extra_options={"tool_choice": "required", "max_tokens": 128},
                tools=[_PROBE_TOOL],
            )
            result["tools"]["native"] = _has_tool_call(response)
            if not result["tools"]["native"]:
                result["tools"]["error"] = "empty_response"
        except ToolsNotSupportedError:
            try:
                fallback = concrete.direct(
                    route.model,
                    "Return only JSON: {\"tool\":\"probe_echo\",\"arguments\":{\"value\":\"ok\"}}",
                    "Use the probe_echo tool.",
                    timeout_sec=timeout,
                    temperature=0.0,
                    max_tokens=128,
                )
                result["tools"]["text_fallback"] = _looks_like_text_tool_call(fallback)
                if not result["tools"]["text_fallback"]:
                    result["tools"]["error"] = "empty_response"
            except Exception as error:
                result["tools"]["error"] = classify_probe_error(error)
        except Exception as error:
            result["tools"]["error"] = classify_probe_error(error)
        result["tools"]["latency_ms"] = _elapsed_ms(started)

    required_stream = "stream" not in route.capabilities or result["stream"]["ok"]
    required_tools = (
        "tools" not in route.capabilities
        or result["tools"]["native"]
        or result["tools"]["text_fallback"]
    )
    result["ok"] = bool(
        result["chat"]["ok"]
        and required_stream
        and required_tools
        and (result["model_exact"] or not result["model_listing"]["supported"])
    )
    result["error_class"] = None if result["ok"] else next((e for e in errors if e), None)
    if result["error_class"] is None and not result["ok"]:
        result["error_class"] = (
            result["tools"].get("error")
            or result["stream"].get("error")
            or result["chat"].get("error")
            or result["model_listing"].get("error")
            or "model_missing"
        )
    result["import_candidate"] = bool(
        result["ok"] and route.provider == "openai_compatible"
    )
    return result


def probe_endpoint(endpoint: EndpointTemplate, values: Mapping[str, str]) -> dict[str, Any]:
    key = str(values.get(endpoint.key_env, "") or "").strip()
    model = str(values.get(endpoint.model_env, "") or "").strip() or endpoint.default_model
    route = Route(
        name=endpoint.name,
        provider="openai_compatible",
        base_url=endpoint.base_url,
        api_key=key,
        model=model,
        tier=Tier.FAST if endpoint.name in {"mistral", "groq"} else Tier.CHAT,
        timeout_sec=8.0,
        capabilities=frozenset({"chat", "stream", "tools"}),
    )
    concrete = OpenAICompatibleBackend(endpoint.base_url, api_key=key or None)
    return probe_route(route, backend=concrete)


def probe_all(values: Mapping[str, str]) -> list[dict[str, Any]]:
    return [probe_endpoint(endpoint, values) for endpoint in ENDPOINTS]


def _save_catalogue(results: list[dict]) -> Path:
    path = Path.home() / ".jarvis" / "llm_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".llm_probe.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"providers": results}, handle, indent=2)
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return path


def main() -> int:
    configure_cli_output()
    values = load_fcc_values()
    if not values:
        print("FCC environment not found", flush=True)
        print(f"Expected: {fcc_env_path()}", flush=True)
        return 1
    print("Probing configured LLM endpoints", flush=True)
    results = probe_all(values)
    for result in results:
        marker = "OK" if result["ok"] else (
            "SKIP" if not result["configured"] else "FAIL"
        )
        print(f"{marker} {result['name']}", flush=True)
        if result.get("error_class"):
            print(f"   {result['error_class']}", flush=True)
    saved = _save_catalogue(results)
    print(f"Probe catalogue saved: {saved}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
